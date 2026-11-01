# Iris Cockpit per Strix Halo

Un container Podman con [Iris](https://github.com/antirez/iris.c) di antirez già compilato per la GPU di Strix Halo (Radeon 8060S, `gfx1151`), e una TUI per installarlo, aggiornarlo, scaricare i modelli e generare immagini. L'impostazione ricalca [AI Toolbox Cockpit](https://github.com/kyuz0/ai-toolbox-cockpit) di kyuz0: Textual, comandi `podman run` espliciti, modelli su cartelle dell'host che sopravvivono agli aggiornamenti.

## Cosa serve sull'host

Una distro con kernel recente e driver `amdgpu` attivo (deve esistere `/dev/kfd`), Podman, Python 3.10+ e pipx. L'utente deve poter aprire `/dev/kfd` e `/dev/dri/renderD*`: su molte distro (Fedora compresa) sono già accessibili a tutti, altrimenti aggiungilo ai gruppi `render` e `video`. La scheda Stato lo verifica:

```bash
sudo usermod -aG render,video "$USER"   # poi logout/login
```

ROCm **non** va installato sull'host: sta tutto nel container (Fedora 44 con i pacchetti ROCm della distro).

## Installazione

Direttamente da GitHub:

```bash
pipx install git+https://github.com/olac-acul/iris-strix-halo.git
iris-cockpit
```

Per aggiornare il cockpit stesso (non Iris, che si aggiorna dalla TUI):

```bash
pipx uninstall iris-cockpit
pipx install git+https://github.com/olac-acul/iris-strix-halo.git
iris-cockpit --version        # la versione compare anche nel sottotitolo della TUI
```

In alternativa, da un clone locale:

```bash
git clone https://github.com/olac-acul/iris-strix-halo.git
pipx install ./iris-strix-halo
```

Al primo avvio la scheda **Stato** mostra cosa manca sull'host. Premi **Installa / aggiorna**: il primo build scarica Fedora e ROCm (alcuni GB) e compila Iris. Va fatto sulla macchina Strix Halo stessa, perché il Makefile di Iris usa `-march=native` per la parte CPU.

Poi nella scheda **Modelli** scarica *FLUX.2 Klein 4B distillato* (non serve token), torna su **Stato** e lancia **Verifica GPU** e **Test rapido**: il test confronta l'output con le immagini di riferimento del repository.

## Come funzionano gli aggiornamenti

All'avvio (e con il tasto `u`) il cockpit confronta il commit di Iris dentro l'immagine con l'ultimo commit del branch su GitHub e, se sei indietro, elenca le novità. Ci sono due livelli di aggiornamento:

- **Installa / aggiorna** ricompila solo Iris all'ultimo commit. Lo strato con Fedora e ROCm resta in cache, quindi ci mette un paio di minuti.
- **Ricostruisci da zero** riscarica l'immagine Fedora e i pacchetti ROCm più recenti, senza cache. Usalo ogni tanto, o quando Fedora pubblica un aggiornamento di ROCm.

Dopo ogni build riuscito le immagini vecchie vengono rimosse. I modelli non vengono mai toccati.

## Dove finiscono i file

| Cosa | Host (default) | Nel container |
|---|---|---|
| Modelli | `~/iris/models` | `/models` |
| Immagini generate | `~/iris/output` | `/output` |
| Sessioni interattive | `~/iris/output/interactive` | `/tmp` |
| Impostazioni | `~/.config/iris-cockpit/config.json` | |

Le cartelle si cambiano dalla scheda **Impostazioni**. Il token Hugging Face per i modelli con licenza (9B, FLUX.3) va inserito al momento del download e non viene salvato; in alternativa esporta `HF_TOKEN` prima di avviare il cockpit.

## Usarlo senza TUI

Il cockpit mostra sempre il comando che esegue. Per esempio, una generazione equivale a:

```bash
podman run --rm -it --device /dev/kfd --device /dev/dri \
  --security-opt seccomp=unconfined --security-opt label=disable \
  --group-add keep-groups \
  -v ~/iris/models:/models -v ~/iris/output:/output \
  localhost/iris-strix-halo:latest \
  iris -d /models/flux-klein-4b -p "a lighthouse at dawn" -W 1024 -H 1024 -o /output/faro.png
```

Dentro il container ci sono anche `iris-download <modello>` (scarica in `/models`) e `iris-selftest`.

## Se qualcosa non va

**La GPU non viene vista nel container.** Con Podman rootless `--group-add keep-groups` richiede il runtime `crun` (default su Fedora). Se usi `runc` o Podman da root, passa ad *Accesso GPU: video + render* in Impostazioni.

**Il build fallisce sui pacchetti ROCm.** I nomi dei pacchetti cambiano tra versioni di Fedora: prova a impostare *Versione Fedora* a quella precedente o successiva e ricostruisci da zero.

**I modelli grandi vanno in out of memory.** Su Strix Halo la GPU usa la memoria GTT, il cui limite dipende dal kernel. La scheda Stato mostra quanta ne vede il driver; AMD documenta lo strumento `amd-ttm` (`pipx install amd-debug-tools`) per alzarlo.
