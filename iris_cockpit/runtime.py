"""Container, configuration and update logic. No UI code here."""

from __future__ import annotations

import grp
import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from importlib import resources
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "iris-cockpit" / "config.json"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

@dataclass
class Config:
    image: str = "localhost/iris-strix-halo:latest"
    iris_repo: str = "https://github.com/antirez/iris.c.git"
    iris_ref: str = "main"
    gpu_arch: str = "gfx1151"
    fedora_version: str = "44"
    models_dir: str = str(Path.home() / "iris" / "models")
    output_dir: str = str(Path.home() / "iris" / "output")
    # "keep-groups" (podman rootless, consigliato) oppure "video-render"
    group_mode: str = "keep-groups"
    engine: str = "podman"

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text())
                known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                return cls(**known)
            except (OSError, ValueError, TypeError):
                pass
        return cls()

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))

    @property
    def models_path(self) -> Path:
        return Path(self.models_dir).expanduser()

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir).expanduser()


# --------------------------------------------------------------------------- #
# Model catalog (directory names match iris.c/download_model.sh)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ModelSpec:
    key: str          # argomento di download_model.sh
    directory: str    # cartella creata dallo script
    name: str
    size_gb: int
    gated: bool
    license: str
    hf_repo: str


MODELS: list[ModelSpec] = [
    ModelSpec("4b", "flux-klein-4b", "FLUX.2 Klein 4B distillato", 16, False, "Apache 2.0",
              "black-forest-labs/FLUX.2-klein-4B"),
    ModelSpec("4b-base", "flux-klein-4b-base", "FLUX.2 Klein 4B base", 16, False, "Apache 2.0",
              "black-forest-labs/FLUX.2-klein-base-4B"),
    ModelSpec("9b", "flux-klein-9b", "FLUX.2 Klein 9B distillato", 30, True, "Non commerciale",
              "black-forest-labs/FLUX.2-klein-9B"),
    ModelSpec("9b-base", "flux-klein-9b-base", "FLUX.2 Klein 9B base", 30, True, "Non commerciale",
              "black-forest-labs/FLUX.2-klein-base-9B"),
    ModelSpec("zimage-turbo", "zimage-turbo", "Z-Image-Turbo 6B", 12, False, "Apache 2.0",
              "Tongyi-MAI/Z-Image-Turbo"),
    ModelSpec("flux3", "flux-3-klein", "FLUX.3 Image Klein distillato", 20, True, "FLUX Kommunity",
              "black-forest-labs/flux-3-image-klein"),
    ModelSpec("flux3-base", "flux-3-klein-base", "FLUX.3 Image Klein base", 20, True, "FLUX Kommunity",
              "black-forest-labs/flux-3-image-klein-base"),
]


def model_status(cfg: Config, spec: ModelSpec) -> str:
    """'scaricato', 'incompleto' o 'assente'."""
    root = cfg.models_path / spec.directory
    if not root.exists():
        return "assente"
    needed = ["transformer", "vae", "text_encoder"]
    for sub in needed:
        d = root / sub
        if not d.is_dir() or not any(d.glob("*.safetensors")):
            return "incompleto"
    if not ((root / "model_index.json").exists() or (root / "modular_model_index.json").exists()):
        return "incompleto"
    return "scaricato"


def dir_size_gb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total / 1024**3


# --------------------------------------------------------------------------- #
# Container engine helpers
# --------------------------------------------------------------------------- #

def engine_available(cfg: Config) -> bool:
    return shutil.which(cfg.engine) is not None


def containerfile_path() -> Path:
    return Path(str(resources.files("iris_cockpit") / "assets" / "Containerfile"))


@dataclass
class ImageInfo:
    present: bool
    commit: str = ""
    ref: str = ""
    created: str = ""
    gpu_arch: str = ""
    fedora: str = ""


def inspect_image(cfg: Config) -> ImageInfo:
    if not engine_available(cfg):
        return ImageInfo(present=False)
    try:
        out = subprocess.run(
            [cfg.engine, "image", "inspect", cfg.image],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ImageInfo(present=False)
    if out.returncode != 0:
        return ImageInfo(present=False)
    try:
        data = json.loads(out.stdout)[0]
    except (ValueError, IndexError):
        return ImageInfo(present=False)
    labels = (data.get("Config") or {}).get("Labels") or data.get("Labels") or {}
    return ImageInfo(
        present=True,
        commit=labels.get("io.iris.commit", ""),
        ref=labels.get("io.iris.ref", ""),
        created=(data.get("Created") or "")[:19].replace("T", " "),
        gpu_arch=labels.get("io.iris.gpu_arch", ""),
        fedora=labels.get("io.iris.fedora", ""),
    )


def gpu_args(cfg: Config) -> list[str]:
    args = [
        "--device", "/dev/kfd",
        "--device", "/dev/dri",
        "--security-opt", "seccomp=unconfined",
        "--security-opt", "label=disable",
    ]
    if cfg.group_mode == "keep-groups":
        args += ["--group-add", "keep-groups"]
    else:
        args += ["--group-add", "video", "--group-add", "render"]
    return args


def run_cmd(cfg: Config, inner: list[str], *, extra_mounts: list[str] | None = None,
            env: dict[str, str] | None = None, workdir: str = "/output",
            name: str | None = None) -> list[str]:
    cfg.models_path.mkdir(parents=True, exist_ok=True)
    cfg.output_path.mkdir(parents=True, exist_ok=True)
    cmd = [cfg.engine, "run", "--rm", "-it"]
    if name:
        cmd += ["--name", name]
    cmd += gpu_args(cfg)
    cmd += ["-v", f"{cfg.models_path}:/models", "-v", f"{cfg.output_path}:/output", "-w", workdir]
    for m in extra_mounts or []:
        cmd += ["-v", m]
    # Le variabili sensibili passano per nome: il valore resta solo nell'ambiente
    # del processo, non compare nella riga di comando mostrata.
    for k in (env or {}):
        cmd += ["-e", k]
    # Passa il tipo di terminale per --show (Kitty/Ghostty/WezTerm/iTerm2)
    for k in ("TERM", "TERM_PROGRAM", "KITTY_WINDOW_ID", "GHOSTTY_RESOURCES_DIR", "WEZTERM_PANE", "KONSOLE_VERSION"):
        if os.environ.get(k):
            cmd += ["-e", k]
    cmd += [cfg.image] + inner
    return cmd


def build_cmd(cfg: Config, commit: str, fresh: bool) -> list[str]:
    cmd = [cfg.engine, "build", "-t", cfg.image,
           "-f", str(containerfile_path()),
           "--build-arg", f"FEDORA_VERSION={cfg.fedora_version}",
           "--build-arg", f"IRIS_REPO={cfg.iris_repo}",
           "--build-arg", f"IRIS_REF={cfg.iris_ref}",
           "--build-arg", f"IRIS_COMMIT={commit or 'unknown'}",
           "--build-arg", f"GPU_ARCH={cfg.gpu_arch}"]
    if fresh:
        cmd += ["--pull=always", "--no-cache"]
    cmd.append(str(containerfile_path().parent))
    return cmd


def prune_cmd(cfg: Config) -> list[str]:
    return [cfg.engine, "image", "prune", "-f", "--filter", "label=io.iris.managed=true"]


def remove_image_cmd(cfg: Config) -> list[str]:
    return [cfg.engine, "rmi", cfg.image]


def pretty(cmd: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


# --------------------------------------------------------------------------- #
# Upstream updates (GitHub API, con fallback a git ls-remote)
# --------------------------------------------------------------------------- #

@dataclass
class RemoteInfo:
    ok: bool
    sha: str = ""
    date: str = ""
    message: str = ""
    error: str = ""
    new_commits: list[str] = field(default_factory=list)


def _github_slug(repo_url: str) -> str | None:
    m = re.match(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?/?$", repo_url)
    return m.group(1) if m else None


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "iris-cockpit",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def check_remote(cfg: Config, installed_sha: str = "") -> RemoteInfo:
    slug = _github_slug(cfg.iris_repo)
    if slug:
        try:
            data = _get_json(f"https://api.github.com/repos/{slug}/commits/{cfg.iris_ref}")
            info = RemoteInfo(
                ok=True,
                sha=data["sha"],
                date=data["commit"]["committer"]["date"][:10],
                message=data["commit"]["message"].splitlines()[0],
            )
            if installed_sha and installed_sha not in ("unknown", info.sha):
                try:
                    cmp = _get_json(f"https://api.github.com/repos/{slug}/compare/{installed_sha}...{info.sha}")
                    info.new_commits = [
                        f"{c['sha'][:7]}  {c['commit']['committer']['date'][:10]}  "
                        f"{c['commit']['message'].splitlines()[0]}"
                        for c in reversed(cmp.get("commits", []))
                    ]
                except Exception:  # noqa: BLE001 - il changelog è facoltativo
                    pass
            return info
        except Exception as e:  # noqa: BLE001
            api_error = str(e)
    else:
        api_error = "repository non su GitHub"
    # Fallback: git ls-remote
    if shutil.which("git"):
        try:
            out = subprocess.run(["git", "ls-remote", cfg.iris_repo, f"refs/heads/{cfg.iris_ref}"],
                                 capture_output=True, text=True, timeout=20)
            if out.returncode == 0 and out.stdout.strip():
                return RemoteInfo(ok=True, sha=out.stdout.split()[0])
        except (OSError, subprocess.TimeoutExpired):
            pass
    return RemoteInfo(ok=False, error=api_error)


# --------------------------------------------------------------------------- #
# Host checks
# --------------------------------------------------------------------------- #

def host_report() -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    rows.append(("/dev/kfd", Path("/dev/kfd").exists(), "presente" if Path("/dev/kfd").exists() else "mancante: driver amdgpu/KFD non attivo"))
    try:
        my_groups = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    except KeyError:
        my_groups = set()
    for g in ("render", "video"):
        ok = g in my_groups
        rows.append((f"gruppo {g}", ok, "ok" if ok else f"aggiungi l'utente: sudo usermod -aG {g} $USER"))
    gtt = None
    for p in Path("/sys/class/drm").glob("card*/device/mem_info_gtt_total"):
        try:
            gtt = max(gtt or 0, int(p.read_text().strip()))
        except (OSError, ValueError):
            pass
    if gtt:
        gb = gtt / 1024**3
        rows.append(("memoria GTT", gb >= 30, f"{gb:.0f} GiB" + ("" if gb >= 30 else " (bassa per i modelli 9B: vedi amd-ttm)")))
    else:
        rows.append(("memoria GTT", False, "non rilevata"))
    rows.append(("kernel", True, os.uname().release))
    return rows


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")
