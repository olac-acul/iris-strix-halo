# Iris Cockpit for Strix Halo

A Podman container with antirez's [Iris](https://github.com/antirez/iris.c) prebuilt for the Strix Halo GPU (Radeon 8060S, `gfx1151`), plus a TUI to install it, update it, download models and generate images. The approach follows kyuz0's [AI Toolbox Cockpit](https://github.com/kyuz0/ai-toolbox-cockpit): Textual, explicit `podman run` commands, and models kept in host folders that survive updates.

## Host requirements

A distro with a recent kernel and the `amdgpu` driver loaded (`/dev/kfd` must exist), Podman, Python 3.10+ and pipx. Your user must be able to open `/dev/kfd` and `/dev/dri/renderD*`. On many distros, Fedora included, these are already accessible to everyone; otherwise add yourself to the `render` and `video` groups. The Status tab checks this for you:

```bash
sudo usermod -aG render,video "$USER"   # then log out and back in
```

You do **not** need ROCm on the host: everything lives in the container (Fedora 44 with the distro's ROCm packages).

## Installation

Straight from GitHub:

```bash
pipx install git+https://github.com/olac-acul/iris-strix-halo.git
iris-cockpit
```

Or from a local clone:

```bash
git clone https://github.com/olac-acul/iris-strix-halo.git
pipx install ./iris-strix-halo
```

After that the cockpit updates itself from the TUI (see *Cockpit updates*).

On first start the **Status** tab shows what is missing on the host. Press **Install / update**: the first build downloads Fedora and ROCm (a few GB) and compiles Iris. Run it on the Strix Halo machine itself, because Iris's Makefile uses `-march=native` for the CPU code.

Then download *FLUX.2 Klein 4B distilled* from the **Models** tab (no token needed), go back to **Status** and run **Check GPU** and **Quick test**. The test compares the output against the reference images in Iris's repository.

## Models

| Model | Size | Token |
|---|---|---|
| FLUX.2 Klein 4B distilled / base | ~16 GB | no |
| Z-Image-Turbo 6B | ~12 GB | no |
| FLUX.2 Klein 9B distilled / base | ~30 GB | yes |
| FLUX.3 Image Klein distilled / base | ~20 GB | yes |

Models marked "yes" are gated on Hugging Face. Accept the license on the model's page, then create a read token at huggingface.co/settings/tokens. Paste the token in the Models tab when downloading; it is never saved. Alternatively, export `HF_TOKEN` before starting the cockpit.

## Generating images

The **Generate** tab runs a single generation with the chosen model, size, steps, seed and optional reference images (img2img / multi-reference). **Interactive session** opens Iris's REPL, which keeps the model loaded between prompts. The REPL starts at the width and height selected in the tab. Inside it, `!size WxH` changes the size, `!help` lists the commands and `!quit` exits.

### Showing images in the terminal

With *Show the image in the terminal* ticked, Iris draws the result directly in the terminal. This needs a terminal that supports the Kitty or iTerm2 graphics protocol:

- **Kitty and Ghostty** work locally and over SSH, because Iris recognizes them from `TERM`. With Kitty you can use `kitten ssh` if the server lacks Kitty's terminfo.
- **WezTerm, iTerm2 and Konsole** work locally. Over SSH their environment variables are not forwarded, so Iris does not recognize them.
- **Alacritty, GNOME Terminal/Ptyxis** and other terminals without these protocols cannot show images.
- **tmux and screen** block the graphics escape codes.

If nothing appears, the image is still saved in `~/iris/output`.

## How updates work

On start (and with the `u` key) the cockpit compares the Iris commit inside the image with the latest commit of the branch on GitHub and, if you are behind, lists what's new. There are two levels of update:

- **Install / update** recompiles only Iris at the latest commit. The Fedora + ROCm layer stays cached, so it takes a couple of minutes.
- **Rebuild from scratch** downloads the latest Fedora image and ROCm packages again, without cache. Use it now and then, or when Fedora ships a ROCm update.

After each successful build, old images are removed. Models are never touched.

The status also says **Rebuild needed** when a newer cockpit adds something to the image (for example the interactive session size). In that case press **Install / update** once.

### Cockpit updates

The cockpit also checks GitHub for a newer version of itself (the *Cockpit* card in the Status tab). It compares version numbers fetched from raw.githubusercontent.com, so it keeps working when the GitHub API rate limit (60 requests/hour without login) is exhausted. When a new version exists, an **Update cockpit** button appears. It reinstalls the package at that exact commit into the same pipx environment (with pip or uv, depending on how pipx created it) and restarts the TUI. You don't need `pipx --force` or `pipx reinstall`, which fail with pipx's uv backend.

Cockpit versions older than 0.3.1 cannot see updates reliably. Update those manually once:

```bash
pipx uninstall iris-cockpit
pipx install git+https://github.com/olac-acul/iris-strix-halo.git
iris-cockpit --version
```

## Where files go

| What | Host (default) | In the container |
|---|---|---|
| Models | `~/iris/models` | `/models` |
| Generated images | `~/iris/output` | `/output` |
| Interactive sessions | `~/iris/output/interactive` | `/tmp` |
| Settings | `~/.config/iris-cockpit/config.json` | |

Folders can be changed in the **Settings** tab.

## Changes to Iris

The image applies a small patch to Iris before compiling ([`patch-iris.sh`](iris_cockpit/assets/patch-iris.sh)): the interactive session reads its starting size from `IRIS_CLI_WIDTH` / `IRIS_CLI_HEIGHT`, while upstream always starts at 256x256. If Iris's code changes and the patch no longer applies, the build still succeeds and the session simply starts at 256x256.

## Using it without the TUI

The cockpit always prints the command it runs. For example, a generation is equivalent to:

```bash
podman run --rm -it --device /dev/kfd --device /dev/dri \
  --security-opt seccomp=unconfined --security-opt label=disable \
  --group-add keep-groups \
  -v ~/iris/models:/models -v ~/iris/output:/output \
  localhost/iris-strix-halo:latest \
  iris -d /models/flux-klein-4b -p "a lighthouse at dawn" -W 1024 -H 1024 -o /output/lighthouse.png
```

The container also provides `iris-download <model>` (downloads into `/models`) and `iris-selftest`.

## Troubleshooting

**The GPU is not visible in the container.** With rootless Podman, `--group-add keep-groups` requires the `crun` runtime (the default on Fedora). If you use `runc` or rootful Podman, switch *GPU access* to *video + render* in Settings.

**The build fails on ROCm packages.** Package names change between Fedora releases: try setting *Fedora version* to the previous or next release and rebuild from scratch.

**Large models run out of memory.** On Strix Halo the GPU uses GTT memory, whose limit depends on the kernel. The Status tab shows how much the driver sees; AMD documents the `amd-ttm` tool (`pipx install amd-debug-tools`) to raise it.
