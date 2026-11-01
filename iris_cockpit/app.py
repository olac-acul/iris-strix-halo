"""Iris Cockpit: TUI per installare, aggiornare e usare Iris su Strix Halo."""

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
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header, Input, Label, Select, Static,
    TabbedContent, TabPane, TextArea,
)

from . import __version__
from .runtime import (
    MODELS, Config, ImageInfo, RemoteInfo, build_cmd, check_remote, dir_size_gb,
    engine_available, host_report, inspect_image, model_status, now_stamp, pretty,
    prune_cmd, remove_image_cmd, run_cmd,
)

SIZES = [256, 512, 768, 1024, 1280, 1536, 1792]


class Confirm(ModalScreen[bool]):
    def __init__(self, question: str, confirm_label: str = "Conferma") -> None:
        super().__init__()
        self.question = question
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Grid(id="dialog"):
            yield Label(self.question, id="question")
            yield Button(self.confirm_label, variant="error", id="yes")
            yield Button("Annulla", variant="primary", id="no")

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class IrisCockpit(App):
    TITLE = "Iris Cockpit"
    SUB_TITLE = "antirez/iris.c su AMD Strix Halo"
    CSS = """
    Screen { background: $surface; }
    #status, #changelog, #host { padding: 0 1; border: round $primary 40%; margin: 0 0 1 0; height: auto; }
    .buttons { height: auto; margin: 0 0 1 0; }
    .buttons Button { margin: 0 1 0 0; }
    .field { height: auto; margin: 0 0 1 0; }
    .field Label { width: 22; padding: 1 1 0 0; }
    .field Input, .field Select { width: 1fr; }
    #prompt { height: 6; margin: 0 0 1 0; }
    #models-table { height: auto; max-height: 14; margin: 0 0 1 0; }
    #gen-log, #models-note, #settings-note { color: $text-muted; padding: 0 1; height: auto; }
    Confirm { align: center middle; }
    #dialog { grid-size: 2; grid-gutter: 1 2; grid-rows: 1fr 3; padding: 1 2;
              width: 64; height: 13; border: thick $error 60%; background: $surface; }
    #question { column-span: 2; height: 1fr; width: 1fr; content-align: center middle; }
    """
    BINDINGS = [
        ("q", "quit", "Esci"),
        ("u", "check_updates", "Controlla aggiornamenti"),
        ("r", "refresh", "Aggiorna stato"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = Config.load()
        self.image = ImageInfo(present=False)
        self.remote = RemoteInfo(ok=False)

    # ------------------------------------------------------------------ UI --

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="tab-status"):
            with TabPane("Stato", id="tab-status"):
                with VerticalScroll():
                    yield Static("Controllo in corso...", id="status")
                    with Horizontal(classes="buttons"):
                        yield Button("Installa / aggiorna", id="build", variant="success")
                        yield Button("Controlla aggiornamenti", id="check")
                        yield Button("Ricostruisci da zero", id="rebuild", variant="warning")
                    with Horizontal(classes="buttons"):
                        yield Button("Verifica GPU", id="gpu")
                        yield Button("Test rapido (4B)", id="selftest")
                        yield Button("Apri shell", id="shell")
                        yield Button("Elimina immagine", id="rmi", variant="error")
                    yield Static("", id="changelog")
                    yield Static("", id="host")
            with TabPane("Modelli", id="tab-models"):
                with VerticalScroll():
                    yield DataTable(id="models-table", cursor_type="row", zebra_stripes=True)
                    with Horizontal(classes="field"):
                        yield Label("Token Hugging Face")
                        yield Input(placeholder="Solo per i modelli con licenza da accettare; non viene salvato",
                                    password=True, id="hf-token")
                    with Horizontal(classes="buttons"):
                        yield Button("Scarica", id="download", variant="success")
                        yield Button("Elimina", id="delete-model", variant="error")
                        yield Button("Aggiorna elenco", id="refresh-models")
                    yield Static("", id="models-note")
            with TabPane("Genera", id="tab-generate"):
                with VerticalScroll():
                    with Horizontal(classes="field"):
                        yield Label("Modello")
                        yield Select([], id="gen-model", prompt="Scarica prima un modello")
                    yield TextArea(id="prompt")
                    with Horizontal(classes="field"):
                        yield Label("Larghezza")
                        yield Select([(str(s), s) for s in SIZES], value=1024, id="gen-w", allow_blank=False)
                        yield Label("Altezza")
                        yield Select([(str(s), s) for s in SIZES], value=1024, id="gen-h", allow_blank=False)
                    with Horizontal(classes="field"):
                        yield Label("Step")
                        yield Input(placeholder="vuoto = default del modello", id="gen-steps", type="integer")
                        yield Label("Seed")
                        yield Input(placeholder="vuoto = casuale", id="gen-seed", type="integer")
                    with Horizontal(classes="field"):
                        yield Label("Riferimenti")
                        yield Input(placeholder="percorsi separati da spazio (img2img / multi-reference)", id="gen-refs")
                    yield Checkbox("Mostra l'immagine nel terminale (Kitty, Ghostty, WezTerm, iTerm2, Konsole)",
                                   id="gen-show")
                    with Horizontal(classes="buttons"):
                        yield Button("Genera", id="generate", variant="success")
                        yield Button("Sessione interattiva", id="interactive")
                    yield Static("", id="gen-log")
            with TabPane("Impostazioni", id="tab-settings"):
                with VerticalScroll():
                    for key, label in [
                        ("models_dir", "Cartella modelli"),
                        ("output_dir", "Cartella immagini"),
                        ("image", "Nome immagine"),
                        ("iris_repo", "Repository Iris"),
                        ("iris_ref", "Branch"),
                        ("gpu_arch", "Architettura GPU"),
                        ("fedora_version", "Versione Fedora"),
                        ("engine", "Motore container"),
                    ]:
                        with Horizontal(classes="field"):
                            yield Label(label)
                            yield Input(value=str(getattr(self.cfg, key)), id=f"set-{key}")
                    with Horizontal(classes="field"):
                        yield Label("Accesso GPU")
                        yield Select([("keep-groups (podman rootless)", "keep-groups"),
                                      ("video + render", "video-render")],
                                     value=self.cfg.group_mode, id="set-group_mode", allow_blank=False)
                    with Horizontal(classes="buttons"):
                        yield Button("Salva impostazioni", id="save-settings", variant="success")
                    yield Static("", id="settings-note")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.add_columns("Modello", "Stato", "Dimensione", "Licenza", "Token")
        self.query_one("#prompt", TextArea).text = "A picture of a lighthouse at dawn, ASA 400 film"
        self.refresh_models()
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

    def _set_status(self, image: ImageInfo, remote: RemoteInfo | None) -> None:
        self.image = image
        if remote is not None:
            self.remote = remote
        t = Text()
        if not engine_available(self.cfg):
            t.append(f"{self.cfg.engine} non trovato. ", style="bold red")
            t.append("Installalo (es. sudo dnf install podman) e riapri il cockpit.")
            self.query_one("#status", Static).update(t)
            return
        if not image.present:
            t.append("Iris non è ancora installato.\n", style="bold yellow")
            t.append("Premi “Installa / aggiorna”: il primo build scarica Fedora e ROCm (alcuni GB) "
                     "e compila Iris per ", style="")
            t.append(self.cfg.gpu_arch, style="bold")
            t.append(".")
        else:
            t.append("Immagine  ", style="dim")
            t.append(f"{self.cfg.image}\n")
            t.append("Creata    ", style="dim")
            t.append(f"{image.created}   Fedora {image.fedora}   {image.gpu_arch}\n")
            t.append("Iris      ", style="dim")
            t.append(f"{image.commit[:7] or '?'}  ({image.ref})\n")
        t.append("GitHub    ", style="dim")
        if remote is None and not self.remote.ok:
            t.append("controllo in corso...")
        elif self.remote.ok:
            t.append(f"{self.remote.sha[:7]}  {self.remote.date}  {self.remote.message}\n")
            if image.present:
                if image.commit == self.remote.sha:
                    t.append("Sei aggiornato all'ultimo commit.", style="bold green")
                elif image.commit in ("", "unknown"):
                    t.append("Versione installata sconosciuta: aggiorna per allinearti.", style="bold yellow")
                else:
                    n = len(self.remote.new_commits)
                    t.append(f"Aggiornamento disponibile{f' ({n} commit)' if n else ''}: "
                             "premi “Installa / aggiorna”.", style="bold yellow")
        else:
            t.append(f"non raggiungibile ({self.remote.error})", style="red")
        self.query_one("#status", Static).update(t)

        cl = self.query_one("#changelog", Static)
        if self.remote.new_commits:
            c = Text()
            c.append("Novità rispetto alla versione installata\n", style="bold")
            for line in self.remote.new_commits[:25]:
                c.append(line + "\n")
            cl.update(c)
            cl.display = True
        else:
            cl.display = False

    def render_host(self) -> None:
        t = Text()
        t.append("Host\n", style="bold")
        for name, ok, detail in host_report():
            t.append("  ✓ " if ok else "  ✗ ", style="green" if ok else "red")
            t.append(f"{name:<14}", style="dim")
            t.append(detail + "\n")
        self.query_one("#host", Static).update(t)

    # ----------------------------------------------------- run helpers --

    def run_suspended(self, cmd: list[str], title: str, env: dict[str, str] | None = None) -> int:
        with self.suspend():
            print(f"\n\033[1m{title}\033[0m\n\033[2m$ {pretty(cmd)}\033[0m\n", flush=True)
            try:
                rc = subprocess.call(cmd, env={**os.environ, **(env or {})})
            except FileNotFoundError:
                print(f"Comando non trovato: {cmd[0]}")
                rc = 127
            except KeyboardInterrupt:
                rc = 130
            colour = "32" if rc == 0 else "31"
            try:
                input(f"\n\033[{colour}m[codice di uscita {rc}]\033[0m Premi Invio per tornare al cockpit...")
            except (EOFError, KeyboardInterrupt):
                pass
        return rc

    def need_image(self) -> bool:
        if not self.image.present:
            self.notify("Installa prima Iris dalla scheda Stato.", severity="warning")
            return False
        return True

    # ---------------------------------------------------- status actions --

    @on(Button.Pressed, "#check")
    def _check(self) -> None:
        self.notify("Controllo aggiornamenti...")
        self.load_status(check_remote_too=True)

    @on(Button.Pressed, "#build")
    def _build(self) -> None:
        self.do_build(fresh=False)

    @on(Button.Pressed, "#rebuild")
    def _rebuild(self) -> None:
        def go(ok: bool | None) -> None:
            if ok:
                self.do_build(fresh=True)
        self.push_screen(Confirm("Ricostruire tutto senza cache? Riscarica Fedora e ROCm aggiornati "
                                 "(più lento, utile dopo aggiornamenti di ROCm).", "Ricostruisci"), go)

    def do_build(self, fresh: bool) -> None:
        if not engine_available(self.cfg):
            self.notify(f"{self.cfg.engine} non è installato.", severity="error")
            return
        with self.suspend():
            print("\nControllo l'ultimo commit di Iris su GitHub...", flush=True)
            remote = check_remote(self.cfg)
            sha = remote.sha if remote.ok else "unknown"
            if not remote.ok:
                print(f"GitHub non raggiungibile ({remote.error}): compilo il branch {self.cfg.iris_ref}.")
        rc = self.run_suspended(build_cmd(self.cfg, sha, fresh),
                                f"Build di Iris {sha[:7]} per {self.cfg.gpu_arch}")
        if rc == 0:
            subprocess.run(prune_cmd(self.cfg), capture_output=True)
            self.notify("Iris installato.", severity="information")
        else:
            self.notify("Build non riuscito: controlla l'output e riprova.", severity="error")
        self.load_status(check_remote_too=True)

    @on(Button.Pressed, "#gpu")
    def _gpu(self) -> None:
        if self.need_image():
            self.run_suspended(run_cmd(self.cfg, ["sh", "-c",
                "rocminfo | grep -E 'Marketing Name|Name:.*gfx' ; echo; "
                "command -v rocm-smi >/dev/null && rocm-smi --showmeminfo vram gtt || true"]),
                "Verifica GPU nel container")

    @on(Button.Pressed, "#selftest")
    def _selftest(self) -> None:
        if not self.need_image():
            return
        if model_status(self.cfg, MODELS[0]) != "scaricato":
            self.notify("Il test usa FLUX.2 Klein 4B distillato: scaricalo dalla scheda Modelli.", severity="warning")
            return
        self.run_suspended(run_cmd(self.cfg, ["iris-selftest", "/models/flux-klein-4b"]),
                           "Test rapido contro i vettori di riferimento")

    @on(Button.Pressed, "#shell")
    def _shell(self) -> None:
        if self.need_image():
            self.run_suspended(run_cmd(self.cfg, ["bash"]),
                               "Shell nel container (exit per uscire). Modelli in /models, immagini in /output")

    @on(Button.Pressed, "#rmi")
    def _rmi(self) -> None:
        if not self.image.present:
            return
        def go(ok: bool | None) -> None:
            if ok:
                out = subprocess.run(remove_image_cmd(self.cfg), capture_output=True, text=True)
                if out.returncode == 0:
                    self.notify("Immagine eliminata. I modelli scaricati restano su disco.")
                else:
                    self.notify(out.stderr.strip() or "Eliminazione non riuscita.", severity="error")
                self.load_status(check_remote_too=False)
        self.push_screen(Confirm(f"Eliminare l'immagine {self.cfg.image}? I modelli restano su disco.",
                                 "Elimina"), go)

    # ------------------------------------------------------------ models --

    def refresh_models(self) -> None:
        table = self.query_one("#models-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        for spec in MODELS:
            status = model_status(self.cfg, spec)
            style = {"scaricato": "green", "incompleto": "yellow", "assente": "dim"}[status]
            size = f"~{spec.size_gb} GB"
            if status != "assente":
                size = f"{dir_size_gb(self.cfg.models_path / spec.directory):.1f} / ~{spec.size_gb} GB"
            table.add_row(spec.name, Text(status, style=style), size, spec.license,
                          "richiesto" if spec.gated else "", key=spec.key)
        if cursor is not None and 0 <= cursor < len(MODELS):
            table.move_cursor(row=cursor)
        try:
            self.cfg.models_path.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(self.cfg.models_path).free
        except OSError:
            free = 0
        self.query_one("#models-note", Static).update(
            f"Cartella: {self.cfg.models_path}   Spazio libero: {free / 1024**3:.0f} GB\n"
            "Per i modelli con token: accetta prima la licenza sulla pagina Hugging Face del modello. "
            "Se un download si interrompe, premi di nuovo Scarica: riparte da capo.")
        sel = self.query_one("#gen-model", Select)
        current = sel.value
        options = [(s.name, s.directory) for s in MODELS if model_status(self.cfg, s) == "scaricato"]
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
            self.notify(f"{spec.name} richiede un token Hugging Face e la licenza accettata su "
                        f"huggingface.co/{spec.hf_repo}", severity="warning", timeout=8)
            return
        env = {"HF_TOKEN": token or os.environ.get("HF_TOKEN", "")} if (token or os.environ.get("HF_TOKEN")) else None
        self.run_suspended(run_cmd(self.cfg, ["iris-download", spec.key], env=env, workdir="/models"),
                           f"Download di {spec.name} (~{spec.size_gb} GB)", env=env)
        self.refresh_models()

    @on(Button.Pressed, "#delete-model")
    def _delete_model(self) -> None:
        spec = self.selected_model()
        if not spec:
            return
        path = self.cfg.models_path / spec.directory
        if not path.exists():
            self.notify("Questo modello non è su disco.")
            return
        def go(ok: bool | None) -> None:
            if ok:
                shutil.rmtree(path, ignore_errors=True)
                self.notify(f"{spec.name} eliminato.")
                self.refresh_models()
        self.push_screen(Confirm(f"Eliminare {spec.name} da {path}?", "Elimina"), go)

    # ---------------------------------------------------------- generate --

    def _model_dir(self) -> str | None:
        val = self.query_one("#gen-model", Select).value
        if val is Select.BLANK or not val:
            self.notify("Scegli un modello scaricato.", severity="warning")
            return None
        return str(val)

    @on(Button.Pressed, "#generate")
    def _generate(self) -> None:
        if not self.need_image():
            return
        model = self._model_dir()
        if not model:
            return
        prompt = self.query_one("#prompt", TextArea).text.strip()
        if not prompt:
            self.notify("Scrivi un prompt.", severity="warning")
            return
        w = self.query_one("#gen-w", Select).value
        h = self.query_one("#gen-h", Select).value
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
                self.notify(f"Immagine non trovata: {p}", severity="error")
                return
            target = f"/refs/{i}_{p.name}"
            mounts.append(f"{p}:{target}:ro")
            inner += ["-i", target]
        if self.query_one("#gen-show", Checkbox).value:
            inner.append("--show")
        rc = self.run_suspended(run_cmd(self.cfg, inner, extra_mounts=mounts), f"Generazione con {model}")
        out = self.cfg.output_path / fname
        log = self.query_one("#gen-log", Static)
        if rc == 0 and out.exists():
            log.update(f"Salvata in {out}")
            self.notify(f"Immagine salvata: {out.name}")
        else:
            log.update("Generazione non riuscita: controlla l'output nel terminale.")

    @on(Button.Pressed, "#interactive")
    def _interactive(self) -> None:
        if not self.need_image():
            return
        model = self._model_dir()
        if not model:
            return
        session = self.cfg.output_path / "interactive"
        session.mkdir(parents=True, exist_ok=True)
        self.run_suspended(
            run_cmd(self.cfg, ["iris", "-d", f"/models/{model}"], extra_mounts=[f"{session}:/tmp"]),
            f"Sessione interattiva con {model} (!help per i comandi, !quit per uscire). "
            f"Immagini in {session}")

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
        self.query_one("#settings-note", Static).update("Impostazioni salvate.")
        self.refresh_models()
        self.load_status(check_remote_too=True)


def main() -> None:
    import sys
    if "--version" in sys.argv:
        print(__version__)
        return
    IrisCockpit().run()


if __name__ == "__main__":
    main()
