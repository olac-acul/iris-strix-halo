"""Iris Cockpit: TUI to install, update and use Iris on Strix Halo."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Input, Label, Select, Static,
    TabbedContent, TabPane, TextArea,
)

from . import __version__
from .runtime import (
    COCKPIT_REPO, DOWNLOADED, INCOMPLETE, MISSING, MODELS, CockpitInfo, Config,
    ImageInfo, RemoteInfo, build_cmd, check_cockpit, check_remote, cockpit_update_cmd,
    dir_size_gb, engine_available, host_report, inspect_image, model_status, now_stamp,
    pretty, prune_cmd, remove_image_cmd, run_cmd,
)

SIZES = [256, 512, 768, 1024, 1280, 1536, 1792]


# Palette (same values as the Textual theme, also used in Rich text)
OK, WARN, ERR, MUTED, ACCENT = "#5EE6A0", "#F5C26B", "#F7768E", "#8B8499", "#A78BFA"

IRIS_THEME = Theme(
    name="iris",
    primary="#A78BFA",
    secondary="#7DD3FC",
    accent="#F472B6",
    foreground="#E6E1F0",
    background="#100E17",
    surface="#17141F",
    panel="#211D2C",
    success=OK,
    warning=WARN,
    error=ERR,
    dark=True,
)


def kv(t: Text, key: str, value: str = "", style: str = "") -> None:
    t.append(f"{key:<10}", style=MUTED)
    t.append(value + "\n", style=style)


def card(widget, title: str):
    widget.border_title = title
    widget.add_class("card")
    return widget


class Confirm(ModalScreen[bool]):
    def __init__(self, question: str, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self.question = question
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question, id="question")
            with Horizontal(id="dialog-buttons"):
                yield Button("Cancel", id="no", compact=True)
                yield Button(self.confirm_label, variant="error", id="yes", compact=True)

    def on_mount(self) -> None:
        self.query_one("#dialog").border_title = "Confirm"

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class IrisCockpit(App):
    TITLE = "Iris Cockpit"
    SUB_TITLE = f"antirez/iris.c on AMD Strix Halo · cockpit v{__version__}"
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (110, "-wide")]
    CSS = """
    Screen { background: $background; align-horizontal: center; }

    /* Centered column with a maximum width */
    #banner, TabbedContent { width: 100%; max-width: 118; }
    TabbedContent { height: 1fr; }
    #banner { height: 3; padding: 1 2 0 2; }
    #brand { width: auto; color: $primary; text-style: bold; }
    #tagline { width: 1fr; content-align: right middle; color: $text-muted; }

    Tabs { margin: 0 1; }
    TabPane { padding: 1 1 0 1; }

    .card {
        height: auto;
        border: round $primary 35%;
        border-title-color: $primary;
        border-title-style: bold;
        background: $surface;
        padding: 0 2;
        margin: 0 0 1 0;
    }
    .card:focus-within { border: round $primary 80%; }

    #overview { grid-size: 2; grid-columns: 3fr 2fr; grid-gutter: 0 1; height: auto; }
    .-narrow #overview { grid-size: 1; grid-columns: 1fr; }
    #overview > .card { height: 100%; }

    .info { height: auto; padding: 1 0 0 0; }
    .buttons { height: auto; padding: 1 0; }
    .buttons Button { margin: 0 2 0 0; }

    .form { grid-size: 4; grid-columns: 13 1fr 13 1fr; grid-gutter: 1 2; grid-rows: auto; height: auto; padding: 1 0; }
    .form.single { grid-size: 2; grid-columns: 18 1fr; }
    .form Label { color: $text-muted; width: 100%; }
    .form .wide { column-span: 3; }
    .form Input, .form Select { width: 100%; }

    #prompt { height: 6; background: $panel; padding: 0 1; }
    #prompt-card { padding: 1 2; }
    #models-table { height: auto; max-height: 12; margin: 1 0; background: $surface; }
    #settings-actions, #settings-note { padding: 0 2; }
    .note { color: $text-muted; height: auto; padding: 0 0 1 0; }
    #gen-show { margin: 0; padding: 0; }

    Confirm { align: center middle; background: $background 70%; }
    #dialog { width: 64; height: auto; padding: 1 2; background: $surface;
              border: round $error 70%; border-title-color: $error; border-title-style: bold; }
    #question { width: 100%; padding: 0 0 1 0; }
    #dialog-buttons { height: auto; align-horizontal: right; }
    #dialog-buttons Button { margin: 0 0 0 2; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("u", "check_updates", "Check for updates"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = Config.load()
        self.image = ImageInfo(present=False)
        self.remote = RemoteInfo(ok=False)
        self.cockpit: CockpitInfo | None = None
        self.restart = False

    # ------------------------------------------------------------------ UI --

    def compose(self) -> ComposeResult:
        with Horizontal(id="banner"):
            yield Static("✿  Iris Cockpit", id="brand")
            yield Static(f"antirez/iris.c · AMD Strix Halo · v{__version__}", id="tagline")
        with TabbedContent(initial="tab-status"):
            with TabPane("Status", id="tab-status"):
                with VerticalScroll():
                    with Grid(id="overview"):
                        with card(Vertical(), "Iris"):
                            yield Static("Checking...", id="status", classes="info")
                            with Horizontal(classes="buttons"):
                                yield Button("Install / update", id="build", variant="primary", compact=True)
                                yield Button("Rebuild from scratch", id="rebuild", variant="warning", compact=True)
                        with card(Vertical(), "Cockpit"):
                            yield Static("Checking...", id="cockpit", classes="info")
                            with Horizontal(classes="buttons"):
                                yield Button("Update cockpit", id="cockpit-update", variant="primary", compact=True)
                    yield card(Static("", id="changelog"), "What's new in Iris")
                    with card(Vertical(), "Tools"):
                        with Horizontal(classes="buttons"):
                            yield Button("Check for updates", id="check", compact=True)
                            yield Button("Check GPU", id="gpu", compact=True)
                            yield Button("Quick test (4B)", id="selftest", compact=True)
                            yield Button("Open shell", id="shell", compact=True)
                            yield Button("Delete image", id="rmi", variant="error", compact=True)
                    yield card(Static("", id="host", classes="info"), "Host")
            with TabPane("Models", id="tab-models"):
                with VerticalScroll():
                    with card(Vertical(), "Models"):
                        yield DataTable(id="models-table", cursor_type="row", zebra_stripes=True)
                        yield Static("", id="models-note", classes="note")
                    with card(Vertical(), "Download"):
                        with Grid(classes="form single"):
                            yield Label("Hugging Face token")
                            yield Input(placeholder="only needed for 9B and FLUX.3; never saved",
                                        password=True, id="hf-token", compact=True)
                        yield Static("", id="token-hint", classes="note")
                        with Horizontal(classes="buttons"):
                            yield Button("Download selected", id="download", variant="primary", compact=True)
                            yield Button("Delete selected", id="delete-model", variant="error", compact=True)
                            yield Button("Refresh list", id="refresh-models", compact=True)
            with TabPane("Generate", id="tab-generate"):
                with VerticalScroll():
                    with card(Vertical(id="prompt-card"), "Prompt"):
                        yield TextArea(id="prompt", compact=True)
                    with card(Vertical(), "Parameters"):
                        with Grid(classes="form"):
                            yield Label("Model")
                            yield Select([], id="gen-model", prompt="Download a model first",
                                         classes="wide", compact=True)
                            yield Label("Width")
                            yield Select([(str(s), s) for s in SIZES], value=1024, id="gen-w",
                                         allow_blank=False, compact=True)
                            yield Label("Height")
                            yield Select([(str(s), s) for s in SIZES], value=1024, id="gen-h",
                                         allow_blank=False, compact=True)
                            yield Label("Steps")
                            yield Input(placeholder="model default", id="gen-steps",
                                        type="integer", compact=True)
                            yield Label("Seed")
                            yield Input(placeholder="random", id="gen-seed", type="integer", compact=True)
                            yield Label("References")
                            yield Input(placeholder="images for img2img / multi-reference, space separated",
                                        id="gen-refs", classes="wide", compact=True)
                        yield Checkbox("Show the image in the terminal (Kitty, Ghostty, WezTerm, iTerm2, Konsole)",
                                       id="gen-show", compact=True)
                        with Horizontal(classes="buttons"):
                            yield Button("Generate", id="generate", variant="primary", compact=True)
                            yield Button("Interactive session", id="interactive", compact=True)
                        yield Static("Width and height also set the starting size of the interactive session.",
                                     id="gen-log", classes="note")
            with TabPane("Settings", id="tab-settings"):
                with VerticalScroll():
                    for title, fields in [
                        ("Folders", [("models_dir", "Models"), ("output_dir", "Images")]),
                        ("Build", [("image", "Image name"), ("iris_repo", "Iris repository"),
                                   ("iris_ref", "Branch"), ("gpu_arch", "GPU architecture"),
                                   ("fedora_version", "Fedora version"), ("engine", "Container engine")]),
                    ]:
                        with card(Vertical(), title):
                            with Grid(classes="form single"):
                                for key, label in fields:
                                    yield Label(label)
                                    yield Input(value=str(getattr(self.cfg, key)), id=f"set-{key}", compact=True)
                                if title == "Build":
                                    yield Label("GPU access")
                                    yield Select([("keep-groups (rootless podman)", "keep-groups"),
                                                  ("video + render", "video-render")],
                                                 value=self.cfg.group_mode, id="set-group_mode",
                                                 allow_blank=False, compact=True)
                    with Horizontal(classes="buttons", id="settings-actions"):
                        yield Button("Save settings", id="save-settings", variant="primary", compact=True)
                    yield Static("", id="settings-note", classes="note")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(IRIS_THEME)
        self.theme = "iris"
        table = self.query_one("#models-table", DataTable)
        table.add_columns("Model", "Status", "Size", "License", "Token")
        self.query_one("#prompt", TextArea).text = "A picture of a lighthouse at dawn, ASA 400 film"
        self.query_one("#cockpit-update", Button).display = False
        self.query_one("#changelog").display = False
        self.refresh_models()
        self.update_token_hint()
        self.render_host()
        self.action_refresh()

    # ------------------------------------------------------------ status --

    def action_refresh(self) -> None:
        self.load_status(check_remote_too=True)

    def action_check_updates(self) -> None:
        self.load_status(check_remote_too=True)

    @work(thread=True, exclusive=True, group="status")
    def load_status(self, check_remote_too: bool = True) -> None:
        image = inspect_image(self.cfg)
        self.call_from_thread(self._set_status, image, None)
        if check_remote_too:
            remote = check_remote(self.cfg, image.commit if image.present else "")
            self.call_from_thread(self._set_status, image, remote)
            cockpit = check_cockpit(__version__)
            self.call_from_thread(self._set_cockpit, cockpit)

    def _set_cockpit(self, info: CockpitInfo) -> None:
        self.cockpit = info
        t = Text()
        kv(t, "Installed", f"v{info.version}  {info.commit[:7] or '(unknown commit)'}")
        r = info.remote
        if not r.ok:
            kv(t, "GitHub", f"unreachable ({r.error})", ERR)
        else:
            kv(t, "GitHub", f"{'v' + info.remote_version if info.remote_version else '?'}  {r.sha[:7]}  {r.date}")
            if info.update_available:
                n = len(r.new_commits)
                t.append(f"● New version available{f' ({n} commits)' if n else ''}", style=f"bold {WARN}")
                for line in r.new_commits[:6]:
                    t.append("\n" + self._short(line.split("  ", 2)[-1], 40), style=MUTED)
            elif not info.verified:
                t.append("● Could not verify: try again later (key u)", style=f"bold {WARN}")
            else:
                t.append("● Up to date", style=f"bold {OK}")
        self.query_one("#cockpit", Static).update(t)
        self.query_one("#cockpit-update", Button).display = info.update_available
        if info.update_available:
            self.notify("A new version of the cockpit is available.", timeout=6)

    @staticmethod
    def _short(text: str, n: int = 50) -> str:
        return text if len(text) <= n else text[: n - 1] + "…"

    def _set_status(self, image: ImageInfo, remote: RemoteInfo | None) -> None:
        self.image = image
        if remote is not None:
            self.remote = remote
        t = Text()
        if not engine_available(self.cfg):
            t.append(f"● {self.cfg.engine} not found\n", style=f"bold {ERR}")
            t.append("Install it (e.g. sudo dnf install podman) and restart the cockpit.", style=MUTED)
            self.query_one("#status", Static).update(t)
            return
        if image.present:
            kv(t, "Image", self.cfg.image)
            kv(t, "Built", f"{image.created}  ·  Fedora {image.fedora}  ·  {image.gpu_arch}")
            kv(t, "Commit", f"{image.commit[:7] or '?'}  ({image.ref})")
        else:
            kv(t, "Image", "not built yet", WARN)
        if remote is None and not self.remote.ok:
            kv(t, "GitHub", "checking...", MUTED)
        elif self.remote.ok:
            kv(t, "GitHub", f"{self.remote.sha[:7]}  {self.remote.date}")
            kv(t, "", self._short(self.remote.message), MUTED)
        else:
            kv(t, "GitHub", f"unreachable ({self.remote.error})", ERR)
        if not image.present:
            t.append("● Not installed\n", style=f"bold {WARN}")
            t.append(f"The first build downloads Fedora and ROCm (a few GB) and compiles Iris for {self.cfg.gpu_arch}.",
                     style=MUTED)
        elif image.outdated:
            t.append("● Rebuild needed\n", style=f"bold {WARN}")
            t.append("This cockpit version adds features to the image: press Install / update.", style=MUTED)
        elif self.remote.ok:
            if image.commit == self.remote.sha or self.remote.status in ("behind", "identical"):
                t.append("● Up to date with the latest commit", style=f"bold {OK}")
            elif image.commit in ("", "unknown"):
                t.append("● Unknown version: update to get in sync", style=f"bold {WARN}")
            else:
                n = len(self.remote.new_commits)
                t.append(f"● Update available{f' ({n} commits)' if n else ''}", style=f"bold {WARN}")
        self.query_one("#status", Static).update(t)

        cl = self.query_one("#changelog", Static)
        if self.remote.new_commits:
            c = Text()
            for line in self.remote.new_commits[:25]:
                sha, date, msg = (line.split("  ", 2) + ["", ""])[:3]
                c.append(f"{sha} ", style=ACCENT)
                c.append(f"{date}  ", style=MUTED)
                c.append(msg + "\n")
            cl.update(c)
            cl.display = True
        else:
            cl.display = False

    def render_host(self) -> None:
        t = Text()
        rows = host_report()
        for i, (name, ok, detail) in enumerate(rows):
            t.append("✓ " if ok else "✗ ", style=f"bold {OK if ok else ERR}")
            t.append(f"{name:<13}", style=MUTED)
            t.append(detail + ("\n" if i < len(rows) - 1 else ""))
        self.query_one("#host", Static).update(t)

    # ----------------------------------------------------- run helpers --

    def run_suspended(self, cmd: list[str], title: str, env: dict[str, str] | None = None) -> int:
        with self.suspend():
            print(f"\n\033[1m{title}\033[0m\n\033[2m$ {pretty(cmd)}\033[0m\n", flush=True)
            try:
                rc = subprocess.call(cmd, env={**os.environ, **(env or {})})
            except FileNotFoundError:
                print(f"Command not found: {cmd[0]}")
                rc = 127
            except KeyboardInterrupt:
                rc = 130
            colour = "32" if rc == 0 else "31"
            try:
                input(f"\n\033[{colour}m[exit code {rc}]\033[0m Press Enter to return to the cockpit...")
            except (EOFError, KeyboardInterrupt):
                pass
        return rc

    def need_image(self) -> bool:
        if not self.image.present:
            self.notify("Install Iris first from the Status tab.", severity="warning")
            return False
        return True

    # ---------------------------------------------------- status actions --

    @on(Button.Pressed, "#check")
    def _check(self) -> None:
        self.notify("Checking for updates...")
        self.load_status(check_remote_too=True)

    @on(Button.Pressed, "#build")
    def _build(self) -> None:
        self.do_build(fresh=False)

    @on(Button.Pressed, "#cockpit-update")
    def _cockpit_update(self) -> None:
        info = self.cockpit
        if not info or not info.update_available:
            return
        cmd = cockpit_update_cmd(info.remote.sha)
        if cmd is None:
            self.notify("Neither pip nor uv is available: update manually with "
                        f"pipx uninstall iris-cockpit && pipx install git+{COCKPIT_REPO}",
                        severity="error", timeout=15)
            return
        rc = self.run_suspended(cmd, f"Updating the cockpit to {info.remote.sha[:7]}")
        if rc == 0:
            self.restart = True
            self.exit()
        else:
            self.notify("Cockpit update failed: check the output.", severity="error")

    @on(Button.Pressed, "#rebuild")
    def _rebuild(self) -> None:
        def go(ok: bool | None) -> None:
            if ok:
                self.do_build(fresh=True)
        self.push_screen(Confirm("Rebuild everything without cache? Downloads the latest Fedora and ROCm "
                                 "(slower, useful after ROCm updates).", "Rebuild"), go)

    def do_build(self, fresh: bool) -> None:
        if not engine_available(self.cfg):
            self.notify(f"{self.cfg.engine} is not installed.", severity="error")
            return
        with self.suspend():
            print("\nLooking up the latest Iris commit on GitHub...", flush=True)
            remote = check_remote(self.cfg)
            sha = remote.sha if remote.ok else "unknown"
            if not remote.ok:
                print(f"GitHub unreachable ({remote.error}): building branch {self.cfg.iris_ref}.")
        rc = self.run_suspended(build_cmd(self.cfg, sha, fresh),
                                f"Building Iris {sha[:7]} for {self.cfg.gpu_arch}")
        if rc == 0:
            subprocess.run(prune_cmd(self.cfg), capture_output=True)
            self.notify("Iris installed.", severity="information")
        else:
            self.notify("Build failed: check the output and try again.", severity="error")
        self.load_status(check_remote_too=True)

    @on(Button.Pressed, "#gpu")
    def _gpu(self) -> None:
        if self.need_image():
            self.run_suspended(run_cmd(self.cfg, ["sh", "-c",
                "rocminfo | grep -E 'Marketing Name|Name:.*gfx' ; echo; "
                "command -v rocm-smi >/dev/null && rocm-smi --showmeminfo vram gtt || true"]),
                "GPU check inside the container")

    @on(Button.Pressed, "#selftest")
    def _selftest(self) -> None:
        if not self.need_image():
            return
        if model_status(self.cfg, MODELS[0]) != DOWNLOADED:
            self.notify("The test uses FLUX.2 Klein 4B distilled: download it from the Models tab.",
                        severity="warning")
            return
        self.run_suspended(run_cmd(self.cfg, ["iris-selftest", "/models/flux-klein-4b"]),
                           "Quick test against the reference images")

    @on(Button.Pressed, "#shell")
    def _shell(self) -> None:
        if self.need_image():
            self.run_suspended(run_cmd(self.cfg, ["bash"]),
                               "Shell in the container (exit to leave). Models in /models, images in /output")

    @on(Button.Pressed, "#rmi")
    def _rmi(self) -> None:
        if not self.image.present:
            return
        def go(ok: bool | None) -> None:
            if ok:
                out = subprocess.run(remove_image_cmd(self.cfg), capture_output=True, text=True)
                if out.returncode == 0:
                    self.notify("Image deleted. Downloaded models stay on disk.")
                else:
                    self.notify(out.stderr.strip() or "Delete failed.", severity="error")
                self.load_status(check_remote_too=False)
        self.push_screen(Confirm(f"Delete the image {self.cfg.image}? Models stay on disk.",
                                 "Delete"), go)

    # ------------------------------------------------------------ models --

    def refresh_models(self) -> None:
        table = self.query_one("#models-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        for spec in MODELS:
            status = model_status(self.cfg, spec)
            style = {DOWNLOADED: OK, INCOMPLETE: WARN, MISSING: MUTED}[status]
            size = f"~{spec.size_gb} GB"
            if status != MISSING:
                size = f"{dir_size_gb(self.cfg.models_path / spec.directory):.1f} / ~{spec.size_gb} GB"
            table.add_row(spec.name, Text("● " + status, style=style), size, spec.license,
                          Text("required", style=WARN) if spec.gated else "", key=spec.key)
        if cursor is not None and 0 <= cursor < len(MODELS):
            table.move_cursor(row=cursor)
        try:
            self.cfg.models_path.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.cfg.models_path).free
        except OSError:
            free = 0
        self.query_one("#models-note", Static).update(
            f"{self.cfg.models_path}  ·  {free / 1024**3:.0f} GB free\n"
            "For models that need a token, accept the license on their Hugging Face page first. "
            "An interrupted download starts over.")
        sel = self.query_one("#gen-model", Select)
        current = sel.value
        options = [(s.name, s.directory) for s in MODELS if model_status(self.cfg, s) == DOWNLOADED]
        sel.set_options(options)
        if current in [o[1] for o in options]:
            sel.value = current
        elif options:
            sel.value = options[0][1]

    def selected_model(self):
        table = self.query_one("#models-table", DataTable)
        if table.cursor_row is None or table.cursor_row >= len(MODELS):
            return None
        return MODELS[table.cursor_row]

    @on(DataTable.RowHighlighted, "#models-table")
    def _model_highlighted(self) -> None:
        self.update_token_hint()

    def update_token_hint(self) -> None:
        spec = self.selected_model()
        if not spec:
            return
        token_input = self.query_one("#hf-token", Input)
        t = Text()
        if spec.gated:
            token_input.disabled = False
            t.append(f"{spec.name}: needs a Hugging Face token. ", style=f"bold {WARN}")
            t.append(f"Accept the license at huggingface.co/{spec.hf_repo}, then create a read "
                     "token at huggingface.co/settings/tokens.", style=MUTED)
            if os.environ.get("HF_TOKEN"):
                t.append(" Leave it empty to use HF_TOKEN from the environment.", style=MUTED)
        else:
            token_input.disabled = True
            t.append(f"{spec.name}: downloads without a token.", style=f"bold {OK}")
        self.query_one("#token-hint", Static).update(t)

    @on(Button.Pressed, "#refresh-models")
    def _refresh_models(self) -> None:
        self.refresh_models()

    @on(Button.Pressed, "#download")
    def _download(self) -> None:
        spec = self.selected_model()
        if not spec or not self.need_image():
            return
        token = self.query_one("#hf-token", Input).value.strip()
        if spec.gated and not token and not os.environ.get("HF_TOKEN"):
            self.notify(f"{spec.name} needs a Hugging Face token and the license accepted at "
                        f"huggingface.co/{spec.hf_repo}", severity="warning", timeout=8)
            return
        env = {"HF_TOKEN": token or os.environ.get("HF_TOKEN", "")} if (token or os.environ.get("HF_TOKEN")) else None
        self.run_suspended(run_cmd(self.cfg, ["iris-download", spec.key], env=env, workdir="/models"),
                           f"Downloading {spec.name} (~{spec.size_gb} GB)", env=env)
        self.refresh_models()

    @on(Button.Pressed, "#delete-model")
    def _delete_model(self) -> None:
        spec = self.selected_model()
        if not spec:
            return
        path = self.cfg.models_path / spec.directory
        if not path.exists():
            self.notify("This model is not on disk.")
            return
        def go(ok: bool | None) -> None:
            if ok:
                shutil.rmtree(path, ignore_errors=True)
                self.notify(f"{spec.name} deleted.")
                self.refresh_models()
        self.push_screen(Confirm(f"Delete {spec.name} from {path}?", "Delete"), go)

    # ---------------------------------------------------------- generate --

    def _model_dir(self) -> str | None:
        val = self.query_one("#gen-model", Select).value
        if val is Select.BLANK or not val:
            self.notify("Pick a downloaded model.", severity="warning")
            return None
        return str(val)

    def _gen_size(self) -> tuple[int, int]:
        return int(self.query_one("#gen-w", Select).value), int(self.query_one("#gen-h", Select).value)

    @on(Button.Pressed, "#generate")
    def _generate(self) -> None:
        if not self.need_image():
            return
        model = self._model_dir()
        if not model:
            return
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if not prompt:
            self.notify("Write a prompt.", severity="warning")
            return
        w, h = self._gen_size()
        fname = f"iris-{now_stamp()}.png"
        inner = ["iris", "-d", f"/models/{model}", "-p", prompt,
                 "-W", str(w), "-H", str(h), "-o", f"/output/{fname}", "-v"]
        steps = self.query_one("#gen-steps", Input).value.strip()
        seed = self.query_one("#gen-seed", Input).value.strip()
        if steps:
            inner += ["-s", steps]
        if seed:
            inner += ["-S", seed]
        mounts = []
        refs = self.query_one("#gen-refs", Input).value.strip()
        for i, ref in enumerate(shlex.split(refs) if refs else []):
            p = Path(ref).expanduser().resolve()
            if not p.is_file():
                self.notify(f"Image not found: {p}", severity="error")
                return
            target = f"/refs/{i}_{p.name}"
            mounts.append(f"{p}:{target}:ro")
            inner += ["-i", target]
        if self.query_one("#gen-show", Checkbox).value:
            inner.append("--show")
        rc = self.run_suspended(run_cmd(self.cfg, inner, extra_mounts=mounts), f"Generating with {model}")
        out = self.cfg.output_path / fname
        log = self.query_one("#gen-log", Static)
        if rc == 0 and out.exists():
            log.update(f"Saved to {out}")
            self.notify(f"Image saved: {out.name}")
        else:
            log.update("Generation failed: check the output in the terminal.")

    @on(Button.Pressed, "#interactive")
    def _interactive(self) -> None:
        if not self.need_image():
            return
        model = self._model_dir()
        if not model:
            return
        w, h = self._gen_size()
        if self.image.outdated:
            self.notify(f"This image predates the size option: the session starts at 256x256. "
                        f"Press Install / update, or type !size {w}x{h} in the session.",
                        severity="warning", timeout=10)
        session = self.cfg.output_path / "interactive"
        session.mkdir(parents=True, exist_ok=True)
        # Read by patch-iris.sh (cli-size) inside the container
        env = {"IRIS_CLI_WIDTH": str(w), "IRIS_CLI_HEIGHT": str(h)}
        self.run_suspended(
            run_cmd(self.cfg, ["iris", "-d", f"/models/{model}"], env=env, extra_mounts=[f"{session}:/tmp"]),
            f"Interactive session with {model} at {w}x{h} (!help for commands, !size WxH to change "
            f"size, !quit to exit). Images in {session}", env=env)

    # ---------------------------------------------------------- settings --

    @on(Button.Pressed, "#save-settings")
    def _save_settings(self) -> None:
        for key in ("models_dir", "output_dir", "image", "iris_repo", "iris_ref",
                    "gpu_arch", "fedora_version", "engine"):
            val = self.query_one(f"#set-{key}", Input).value.strip()
            if val:
                setattr(self.cfg, key, val)
        self.cfg.group_mode = str(self.query_one("#set-group_mode", Select).value)
        self.cfg.save()
        self.query_one("#settings-note", Static).update("Settings saved.")
        self.refresh_models()
        self.load_status(check_remote_too=True)


def main() -> None:
    import sys
    if "--version" in sys.argv:
        print(__version__)
        return
    app = IrisCockpit()
    app.run()
    if app.restart:
        # Restart with the freshly installed code
        os.execv(sys.executable, [sys.executable, "-m", "iris_cockpit", *sys.argv[1:]])


if __name__ == "__main__":
    main()
