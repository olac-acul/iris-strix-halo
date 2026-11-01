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

# Revision of what the cockpit puts into the image on top of upstream Iris
# (Containerfile + patch-iris.sh). Bump it whenever those change, so the
# cockpit can tell the user to rebuild an older image.
#   1: initial image
#   2: patch cli-size (interactive session starts at IRIS_CLI_WIDTH x IRIS_CLI_HEIGHT)
IMAGE_REV = "2"


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
    # "keep-groups" (rootless podman, recommended) or "video-render"
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
    key: str          # download_model.sh argument
    directory: str    # directory created by the script
    name: str
    size_gb: int
    gated: bool       # Hugging Face requires accepting the license (needs a token)
    license: str
    hf_repo: str


MODELS: list[ModelSpec] = [
    ModelSpec("4b", "flux-klein-4b", "FLUX.2 Klein 4B distilled", 16, False, "Apache 2.0",
              "black-forest-labs/FLUX.2-klein-4B"),
    ModelSpec("4b-base", "flux-klein-4b-base", "FLUX.2 Klein 4B base", 16, False, "Apache 2.0",
              "black-forest-labs/FLUX.2-klein-base-4B"),
    ModelSpec("9b", "flux-klein-9b", "FLUX.2 Klein 9B distilled", 30, True, "Non-commercial",
              "black-forest-labs/FLUX.2-klein-9B"),
    ModelSpec("9b-base", "flux-klein-9b-base", "FLUX.2 Klein 9B base", 30, True, "Non-commercial",
              "black-forest-labs/FLUX.2-klein-base-9B"),
    ModelSpec("zimage-turbo", "zimage-turbo", "Z-Image-Turbo 6B", 12, False, "Apache 2.0",
              "Tongyi-MAI/Z-Image-Turbo"),
    ModelSpec("flux3", "flux-3-klein", "FLUX.3 Image Klein distilled", 20, True, "FLUX Kommunity",
              "black-forest-labs/flux-3-image-klein"),
    ModelSpec("flux3-base", "flux-3-klein-base", "FLUX.3 Image Klein base", 20, True, "FLUX Kommunity",
              "black-forest-labs/flux-3-image-klein-base"),
]

DOWNLOADED, INCOMPLETE, MISSING = "downloaded", "incomplete", "missing"


def model_status(cfg: Config, spec: ModelSpec) -> str:
    """DOWNLOADED, INCOMPLETE or MISSING."""
    root = cfg.models_path / spec.directory
    if not root.exists():
        return MISSING
    for sub in ("transformer", "vae", "text_encoder"):
        d = root / sub
        if not d.is_dir() or not any(d.glob("*.safetensors")):
            return INCOMPLETE
    if not ((root / "model_index.json").exists() or (root / "modular_model_index.json").exists()):
        return INCOMPLETE
    return DOWNLOADED


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
    cockpit_rev: str = ""   # IMAGE_REV the image was built with ("" = before revisions existed)

    @property
    def outdated(self) -> bool:
        """Built by an older cockpit: missing features until rebuilt."""
        return self.present and self.cockpit_rev != IMAGE_REV


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
        cockpit_rev=labels.get("io.iris.cockpit_rev", ""),
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
    # Variables are passed by name: values (e.g. tokens) stay in the process
    # environment and never appear in the command line shown to the user.
    for k in (env or {}):
        cmd += ["-e", k]
    # Terminal type, so that --show can pick Kitty/Ghostty/WezTerm/iTerm2/Konsole
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
           "--build-arg", f"GPU_ARCH={cfg.gpu_arch}",
           "--build-arg", f"COCKPIT_REV={IMAGE_REV}"]
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
# Upstream updates (GitHub API, falling back to git ls-remote)
# --------------------------------------------------------------------------- #

@dataclass
class RemoteInfo:
    ok: bool
    sha: str = ""
    date: str = ""
    message: str = ""
    error: str = ""
    # Result of comparing installed...remote: "ahead" = remote is newer.
    # The unauthenticated API can return a stale commit for about a minute:
    # without this check we could "update" to an older version.
    status: str = ""
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
                    info.status = cmp.get("status", "")
                    info.new_commits = [
                        f"{c['sha'][:7]}  {c['commit']['committer']['date'][:10]}  "
                        f"{c['commit']['message'].splitlines()[0]}"
                        for c in reversed(cmp.get("commits", []))
                    ]
                except Exception:  # noqa: BLE001 - the changelog is optional
                    pass
            return info
        except Exception as e:  # noqa: BLE001
            api_error = str(e)
    else:
        api_error = "repository is not on GitHub"
    # Fallback: git ls-remote (not subject to the API rate limit)
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
# Updating the cockpit itself
# --------------------------------------------------------------------------- #

COCKPIT_REPO = "https://github.com/olac-acul/iris-strix-halo.git"
COCKPIT_REF = "main"


@dataclass
class CockpitInfo:
    version: str
    commit: str        # installed commit, "" if unknown (e.g. installed from a local folder)
    remote: RemoteInfo
    remote_version: str = ""   # __version__ read from the remote commit

    @property
    def update_available(self) -> bool:
        r = self.remote
        if not (r.ok and r.sha) or self.commit == r.sha:
            return False
        # Main signal: a higher version number on GitHub. Independent of the
        # API (60 requests/hour without login) and of its cache.
        if self.remote_version and _vtuple(self.remote_version) > _vtuple(self.version):
            return True
        # Unknown installed commit (installed from a folder): align with GitHub.
        return not self.commit or r.status == "ahead"

    @property
    def verified(self) -> bool:
        """True if we could establish whether an update exists."""
        return bool(self.remote.ok and (self.remote_version or self.remote.status
                                        or self.commit == self.remote.sha))


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def remote_cockpit_version(sha: str) -> str:
    """Read __version__ at commit `sha` from raw.githubusercontent.com (outside the API rate limit)."""
    slug = _github_slug(COCKPIT_REPO)
    url = f"https://raw.githubusercontent.com/{slug}/{sha}/iris_cockpit/__init__.py"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "iris-cockpit"})
        with urllib.request.urlopen(req, timeout=10) as r:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', r.read().decode())
            return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def installed_cockpit_commit() -> str:
    """Commit the package was installed from (PEP 610, written by pip and uv)."""
    from importlib.metadata import PackageNotFoundError, distribution
    try:
        raw = distribution("iris-cockpit").read_text("direct_url.json")
    except PackageNotFoundError:
        return ""
    try:
        return (json.loads(raw or "{}").get("vcs_info") or {}).get("commit_id", "")
    except ValueError:
        return ""


def check_cockpit(version: str) -> CockpitInfo:
    cfg = Config(iris_repo=COCKPIT_REPO, iris_ref=COCKPIT_REF)
    commit = installed_cockpit_commit()
    remote = check_remote(cfg, commit)
    rv = remote_cockpit_version(remote.sha) if remote.ok and remote.sha else ""
    return CockpitInfo(version=version, commit=commit, remote=remote, remote_version=rv)


def cockpit_update_cmd(sha: str) -> list[str] | None:
    """Reinstall the cockpit at commit `sha` into the environment it runs in.

    Pinning the commit stops pip/uv from reusing a cached build, and avoids
    `pipx install --force`, which fails with pipx's uv backend.
    """
    import importlib.util
    import sys
    spec = f"iris-cockpit @ git+{COCKPIT_REPO}@{sha}"
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                "--no-cache-dir", "--force-reinstall", spec]
    if shutil.which("uv"):
        return ["uv", "pip", "install", "--python", sys.executable,
                "--reinstall-package", "iris-cockpit", spec]
    return None


# --------------------------------------------------------------------------- #
# Host checks
# --------------------------------------------------------------------------- #

def _can_rw(path: Path) -> bool:
    return os.access(path, os.R_OK | os.W_OK)


def host_report() -> list[tuple[str, bool, str]]:
    rows: list[tuple[str, bool, str]] = []
    fix = "sudo usermod -aG render,video $USER, then log out and back in"

    # Check the actual permission on the devices, not group membership: on
    # many distros udev rules make them accessible to everyone (0666).
    kfd = Path("/dev/kfd")
    kfd_ok = kfd.exists() and _can_rw(kfd)
    if not kfd.exists():
        rows.append(("/dev/kfd", False, "missing: amdgpu/KFD driver not active"))
    else:
        rows.append(("/dev/kfd", kfd_ok, "accessible" if kfd_ok else f"not accessible: {fix}"))

    nodes = sorted(Path("/dev/dri").glob("renderD*"))
    usable = [n for n in nodes if _can_rw(n)]
    if not nodes:
        rows.append(("render node", False, "no /dev/dri/renderD*: amdgpu driver not active"))
    elif usable:
        rows.append(("render node", True, f"{', '.join(n.name for n in usable)} accessible"))
    else:
        rows.append(("render node", False, f"{', '.join(n.name for n in nodes)} not accessible: {fix}"))

    try:
        my_groups = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    except KeyError:
        my_groups = set()
    missing = [g for g in ("render", "video") if g not in my_groups]
    if not missing:
        rows.append(("groups", True, "render, video"))
    elif kfd_ok and usable:
        rows.append(("groups", True, f"not in {', '.join(missing)}, but not needed: the devices are accessible"))
    else:
        rows.append(("groups", False, f"missing {', '.join(missing)}: {fix}"))
    gtt = None
    for p in Path("/sys/class/drm").glob("card*/device/mem_info_gtt_total"):
        try:
            gtt = max(gtt or 0, int(p.read_text().strip()))
        except (OSError, ValueError):
            pass
    if gtt:
        gb = gtt / 1024**3
        rows.append(("GTT memory", gb >= 30, f"{gb:.0f} GiB" + ("" if gb >= 30 else " (low for 9B models: see amd-ttm)")))
    else:
        rows.append(("GTT memory", False, "not detected"))
    rows.append(("kernel", True, os.uname().release))
    return rows


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")
