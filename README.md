<div align="center">

**English** | [简体中文](README_ZH.md)

# 🚂 MagicLingua

**Local-first AI translation infrastructure** — a translation runtime that runs entirely on your own machine, served over an OpenAI-compatible local API.

Ship it with a browser client and a document pipeline — or skip both and call the API from your own tools.

No account · Nothing uploaded · Free forever · Open source

[![CI](https://github.com/jinleiviva/magiclingua/actions/workflows/ci.yml/badge.svg)](https://github.com/jinleiviva/magiclingua/actions/workflows/ci.yml)
[![version](https://img.shields.io/badge/version-2.0.0-blue)](https://github.com/jinleiviva/magiclingua/releases)
[![license](https://img.shields.io/badge/code%20license-MIT-green)](LICENSE)
[![platform](https://img.shields.io/badge/platform-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)](#getting-started)
[![price](https://img.shields.io/badge/price-free%20forever-brightgreen)](LICENSE)

</div>

> ⚠️ **Regions of use**: The bundled translation model is licensed under the Tencent HY Community License Agreement, which **does not apply to the European Union, the United Kingdom, or South Korea**. Please do not use or distribute it in those regions. Full license text: [`MODEL_LICENSE.txt`](MODEL_LICENSE.txt).

---

## Table of Contents

- [What is this](#what-is-this)
- [Architecture](#architecture)
- [Use it without the extension — the local API](#use-it-without-the-extension--the-local-api)
- [What the clients do](#what-the-clients-do)
- [Screenshots](#screenshots)
- [Getting started](#getting-started)
- [Daily usage](#daily-usage)
- [FAQ](#faq)
- [For developers](#for-developers)
- [Contributing](#contributing)
- [Acknowledgements & License](#acknowledgements--license)

---

## What is this

MagicLingua is **local-first translation infrastructure**. At its core sits a small local server — the **Local Translation Runtime** — that loads a translation model (Tencent HY-MT2) on your own machine and exposes it through an **OpenAI-compatible API** on `localhost:18770`.

Everything else in this repo is a client built on top of that runtime:

- 🌐 **A browser client** (Chrome extension) — full-page web translation, selection & hover translation, bilingual YouTube subtitles
- 📄 **A document pipeline** — layout-preserving PDF translation (via BabelDOC), plus EPUB / TXT / SRT / ASS with bilingual output
- 🔌 **Any OpenAI-SDK app** — point it at `http://localhost:18770/v1` and it just works

Because the runtime speaks the OpenAI chat-completions protocol, third-party tools don't need to know (or care) that a 1.8B GGUF model is behind it. In fact, BabelDOC already consumes this exact endpoint as its translation backend — the API isn't an afterthought, it's the seam the project is built around.

Unlike typical cloud translation services, everything runs on your own computer:

- 🔒 **Private by design** — Translation happens locally. The pages you browse, the documents you upload, and every API call **never leave your computer**
- 💰 **Free forever** — No sign-up, no subscription, no ads, no usage meter. Open source (MIT for the code)
- 🌍 **38 languages** — Chinese / English / Japanese / Korean / French / German / Spanish / Russian / Arabic / Portuguese and more
- 💻 **Runs on ordinary hardware** — The translation model is about 1.1 GB; any computer from the last five years (dedicated GPU or not) can run it. Apple Silicon gets Metal acceleration out of the box

**Why I built it**: I read a lot of English content every day, and at some point I realized that commercial-grade translation quality had become something my own computer could run for free. So I built the runtime first, then the clients I personally needed. The Chrome extension is just the first client — the runtime is meant to be reused by anything you want to plug into it.

---

## Architecture

```
                          MagicLingua
                               │
                  Local Translation Runtime
                 (HY-MT2 · llama.cpp · your machine)
                               │
                   OpenAI-compatible local API
                http://localhost:18770/v1/...
                               │
        ┌──────────────────────┼──────────────────────┐
        ▼                      ▼                      ▼
   Browser client        Document pipeline      Your own tools
   (Chrome extension)    (PDF / EPUB /          (OpenAI SDK,
        │                 TXT / SRT / ASS)      scripts, apps)
        ▼
  Web pages · YouTube
  Selection & hover
```

One runtime, many clients. The browser extension can start and stop the runtime with one click (via native messaging), the document pipeline shares the same model instance, and anything that speaks the OpenAI protocol can join without asking permission.

---

## Use it without the extension — the local API

You don't need the Chrome extension at all. Once the local service is running (see [Getting started](#getting-started)), the runtime exposes a standard OpenAI-compatible API:

**Check the runtime is up:**

```bash
curl http://localhost:18770/health
```

**OpenAI-compatible endpoint** — works with the OpenAI SDK, LangChain, or any tool that accepts a custom `base_url`:

```bash
curl http://localhost:18770/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [
      {"role": "system", "content": "Translate the following text into French."},
      {"role": "user", "content": "Good morning! The meeting starts at nine."}
    ],
    "stream": false
  }'
```

**Or with the OpenAI Python SDK:**

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:18770/v1", api_key="local")

resp = client.chat.completions.create(
    model="magiclingua",  # model name is ignored; the runtime loads its own model
    messages=[{"role": "user", "content": "Translate into Japanese: Hello, world."}],
)
print(resp.choices[0].message.content)
```

**Structured translation endpoint** — if you'd rather not manage prompts yourself, `/v1/translate` takes raw text and the server builds the translation prompt (glossary support, context passing, SSE streaming):

```bash
curl http://localhost:18770/v1/translate \
  -H "Content-Type: application/json" \
  -d '{"text": "A stitch in time saves nine.", "target_lang": "zh"}'
```

Other endpoints: `/v1/models` (model listing), `/v1/translate/batch`, and the document job endpoints under `/v1/pdf/*` that power the web UI. The API is unauthenticated and binds to localhost only — it is meant for local use, not for serving other machines.

---

## What the clients do

| What you want | How |
| --- | --- |
| Read an English web page | Open the page → click the extension icon → toggle **Enable Translation**. The whole page is translated in place, layout intact; toggle again to restore the original |
| Read a paragraph | Select the text, click the blue "Translate" button that appears / use the right-click menu / press `Alt+T` — the translation shows up instantly |
| Watch YouTube videos | Turn on captions (CC) in the player; the extension shows the original and the translation side by side |
| Watch Bilibili / Netflix etc. | Turn on the video's original captions; the extension translates along as they change (these sites are experimental support) |
| Translate a PDF article | Open `http://localhost:18770/pdf` in your browser, upload the PDF, pick the articles you want, translate and download — layout and images preserved |
| Translate e-books / text files / subtitle files | Same page: pick the file type (EPUB / TXT / SRT / ASS), drop the file in, download when done. Bilingual output by default |
| Get domain terms right | Built-in glossaries for finance / tech / business (just tick to enable), or add your own terms — consistent wording across the whole document |
| Adjust how translations look | In settings, change subtitle background and text color; 4 ready-made color themes with one click |

---

## Screenshots

<table>
  <tr>
    <td width="50%" align="center">
      <a href="docs/screenshot-web.png">
        <img src="docs/screenshot-web.png" width="340" alt="Full-page web translation">
      </a>
      <br>
      <sub>🌍 Full-page translation · Google News replaced in place, layout intact</sub>
    </td>
    <td width="50%" align="center">
      <a href="docs/screenshot-youtube.jpg">
        <img src="docs/screenshot-youtube.jpg" width="340" alt="YouTube bilingual subtitles">
      </a>
      <br>
      <sub>🎬 YouTube bilingual subtitles · original and translation side by side</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <a href="docs/screenshot-pdf.png">
        <img src="docs/screenshot-pdf.png" width="300" alt="PDF translation assistant">
      </a>
      <br>
      <sub>📄 PDF translation assistant · table of contents parsed automatically</sub>
    </td>
    <td width="50%" align="center">
      <a href="docs/screenshot-pdf-result.png">
        <img src="docs/screenshot-pdf-result.png" width="300" alt="PDF translation result">
      </a>
      <br>
      <sub>📑 PDF translation result · layout, images, and columns preserved</sub>
    </td>
  </tr>
</table>

<p align="center">
  <sub>⚙️ Extension popup: translation toggle + one-click local server start/stop (click for full size)</sub>
</p>
<p align="center">
  <a href="docs/screenshot-popup.png">
    <img src="docs/screenshot-popup.png" width="220" alt="Extension popup">
  </a>
</p>

---

## Getting started

Two steps: **install the local translation runtime (one click) → install the browser client**. Step 1 alone is enough if you only want the API.

> Requirements: **macOS (Apple Silicon)**, 4 GB+ RAM, about 3 GB of disk space. No GPU needed.
> See [For developers](#for-developers) at the end for Windows / Linux. Intel Macs automatically fall back to building from source (takes longer).

### Step 1: Install the local translation runtime (once, a few minutes)

**macOS — recommended: double-click** (no terminal needed):

```
Download the repo → double-click install.command
```

Or open a terminal (more reliable, shows progress):

```bash
git clone https://github.com/jinleiviva/magiclingua.git && cd magiclingua
./install.command
```

The script **does everything automatically**:

1. Creates a Python virtual environment
2. Installs the translation engine — on Apple Silicon it uses the official prebuilt wheel (**no compiling from source**; the old 7-minute build is no longer needed)
3. Downloads the translation model from **ModelScope** (about 1.1 GB, fast direct connection in China, with progress bar and resume support)
4. Registers auto-start on login (launchd) + the browser native host (so the extension popup can start/stop the service with one click)

When you see `MagicLingua 安装完成` (installation complete), you're done. Verify with `curl http://localhost:18770/health`. **If you're installing the extension too, fully quit Chrome (Cmd+Q) and reopen it.**

> Manual step-by-step install (without the one-click script):
> ```bash
> ./setup_env.sh          # environment + engine + dependencies
> ./download_model.sh     # download the model (about 1.1 GB)
> ./start_server_gguf.sh  # start the service
> ```

### Step 2: Install the browser extension (once)

1. Open `chrome://extensions` in Chrome
2. Turn on **Developer mode** in the top-right corner
3. Click **Load unpacked** and select the `extension` folder in the repo

Once installed, the extension icon appears in your browser toolbar. Click it — if you can see the popup panel, you're set.

> Step 1 registered the native host, so after **restarting Chrome**, the Start / Stop buttons in the popup control the service — no terminal needed.

<details>
<summary>More notes (Windows / Linux, what if the extension ID changes)</summary>

- **Windows / Linux one-click start/stop**: no install script yet; you need to register the native host manually (see the instructions in `native_host/com.magiclingua.host.json`). Not registering doesn't affect translation itself.
- **Extension ID changed**: the extension ID is derived from the location of the `extension` folder — as long as you don't move or rename it, it stays the same. If it ever changes, just re-run `./native_host/install.command` to re-register automatically.
- **Port already in use**: the default port is 18770. If it's taken, find and kill the process with `lsof -i :18770`, or set the `HYMT_PORT` environment variable to switch ports.

</details>

---

## Daily usage

### Translating web pages

Click the extension icon → toggle **Enable Translation**. The current page starts translating right away: each translation shows a spinner first, then replaces the original text in place. Toggle the switch again and all translations disappear, restoring the original page.

**Auto-translate sites you visit often**: in the popup, set "This site" to **Auto-translate**. From then on, every new page on that site is translated automatically — no buttons needed (switch back to "Translate on click" or "Don't translate" anytime).

Pages you've translated before load instantly when reopened (local cache).

### Translating a piece of text

- **Selection**: select some text, click the blue "Translate" button next to the selection, or choose "Translate selection" from the right-click menu, or press `Alt+T` (the shortcut can be changed in your browser's extension shortcut settings)
- **Hover**: hold `Ctrl` and rest the mouse on a paragraph — the translation appears in place; move the mouse away or release the key and it disappears (don't like `Ctrl`? Switch to `Alt` in settings)

### Watching videos

- **YouTube**: turn on captions (CC) in the player; the extension translates ahead of time and shows the original and translation side by side
- **Bilibili / Netflix / Coursera / Udemy** (experimental): turn on the video's original captions the same way; as captions change, the translation follows
- **Export subtitles**: on a video page, click "Export subtitles" in the popup to get an `.srt` file you can drop straight into CapCut and similar tools

### Translating documents and e-books

Open **`http://localhost:18770/pdf`** in your browser (available while the service is running):

- **PDF**: upload and the table of contents is listed automatically → tick the articles you want → translate → download. Only the pages you ticked are translated; scanned PDFs work too (text recognized automatically). If the table of contents is detected incorrectly, you can enter page ranges manually (e.g. `1,3-5`)
- **EPUB / TXT / subtitle files**: pick the file type, drop the file in → download when done. Output is **bilingual** (original + translation) by default; you can switch to translation-only. Long documents are translated in segments, and a failed segment doesn't affect the rest

### What's in Settings

Click the extension icon → **⚙️** in the top-right corner:

| Setting | What it does |
| --- | --- |
| This site | Three options for the current site: **Auto-translate** (new pages translate automatically) / **Translate on click** / **Don't translate** |
| Idle strategy | **Power saving** (default; releases model memory after 20 min idle) or **Stay resident** (model stays loaded, translations are instant) |
| Translation style | Subtitle background / text color; 4 ready-made themes; web-page translations follow the color |
| Glossary | Tick built-in glossaries (finance / tech / business) or add your own terms; batch import/export via CSV |
| Hover trigger key | `Ctrl` or `Alt` |
| Target language / display mode | Which language to translate into; bilingual or translation-only |

---

## FAQ

- **Service won't start?** Check in order: is the model downloaded (`ls models/*.gguf`), are the dependencies installed (`venv/bin/python -m pip list | grep llama-cpp`), is the port taken (`lsof -i :18770`), then check the log `tail -f log_server.txt` for the cause.
- **"Model not found"?** Run `./download_model.sh` once; or point to the file yourself: `export HYMT_MODEL_PATH=/your/model/path.gguf`.
- **Translation is slow?** The first translation loads the model and takes a bit longer (10–20 seconds); after that it's fast. Translated content is cached — repeat translations are instant. Swapping in a larger or smaller model trades quality against speed (see [For developers](#for-developers)).
- **Will my computer run it?** Any Mac / Windows / Linux machine from the last five years. Faster with an NVIDIA GPU, but pure CPU works fine. Phones and browser-only environments are not supported.
- **Translation quality is meh?** The 1.8B model is small and fast, with quality close to commercial services; for better results, switch to a larger model (7B) at the cost of speed.
- **Why isn't some site supported?** Web-page translation is generic — "Enable Translation" works on the vast majority of sites; a few special sites (video sites, unusual layouts) need tailored adapters. Tell us which sites you frequent and we'll see what we can do.

---

## For developers

<details>
<summary>Environment variables & model configuration</summary>

| Variable | Default | Description |
| --- | --- | --- |
| `HYMT_PORT` | `18770` | Service port |
| `HYMT_IDLE_EXIT` | `20` | Minutes of idle before auto-exit; `0` = stay resident |
| `HYMT_MODEL_PATH` | empty | Explicit path to the model file (takes precedence over `models/`) |
| `MODEL_NAMESPACE` / `MODEL_REPO` / `MODEL_FILE` | Tencent-Hunyuan / Hy-MT2-1.8B-GGUF / Q4_K_M | Download target for `download_model.sh` (ModelScope by default) |
| `HF_ENDPOINT` | empty | When set, switches the download source to HuggingFace / a mirror (e.g. `https://huggingface.co`) |

Switch to a larger model (better quality, slower) — ModelScope by default:

```bash
MODEL_NAMESPACE=Tencent-Hunyuan MODEL_REPO=Hy-MT2-7B-GGUF MODEL_FILE=Hy-MT2-7B-Q4_K_M.gguf ./download_model.sh
```

Switch back to the official HuggingFace source:

```bash
HF_ENDPOINT=https://huggingface.co MODEL_NAMESPACE=tencent MODEL_REPO=Hy-MT2-7B-GGUF MODEL_FILE=Hy-MT2-7B-Q4_K_M.gguf ./download_model.sh
```

Apple Silicon acceleration: the bundled dependency install enables Metal; when building yourself, set `CMAKE_ARGS="-DGGML_METAL=ON"`.

</details>

<details>
<summary>Project structure</summary>

```
magiclingua/
├── install.command         # macOS double-click one-click install (entry point for regular users) ⭐
├── setup_env.sh            # one-click setup script (venv + engine + model + auto-start registration)
├── download_model.sh       # download the model into models/ (ModelScope by default, resumable)
├── requirements-core.txt   # core translation dependencies (no PDF parsing components)
├── requirements-pdf.txt    # PDF translation dependencies (installed on demand via --with-pdf)
├── start_server_gguf.sh    # manually start the local translation runtime (dev / debug)
├── server_gguf.py          # the runtime: OpenAI-compatible API + PDF/document translation ⭐
├── ocr_engine.py           # OCR for scanned PDFs (macOS Vision)
├── pdf_toc.py              # PDF table-of-contents parsing
├── requirements.txt        # full runtime dependencies (incl. PDF, pinned versions)
├── config.example.json     # sample config (config.json is generated on first run)
├── test_api.py             # API smoke tests
├── test_streaming.py       # streaming smoke tests
├── pack_extension.py       # pack the CRX (for releases)
├── ui-preview.html         # popup style preview page (dev)
├── icons_src/              # icon source assets
├── extension/              # browser client: Chrome extension (load unpacked)
├── native_host/            # Native Messaging channel between extension and local runtime
├── LICENSE                 # license for this project's code (MIT)
├── MODEL_LICENSE.txt       # translation model license (Tencent HY Community License)
└── NOTICE                  # consolidated license & trademark notices
```

</details>

<details>
<summary>Technical notes</summary>

- The runtime (`server_gguf.py`) is the core: model loading, prompt construction, glossary merging, caching, and the OpenAI-compatible API layer.
- The browser client has three layers: site configuration (`site_registry.js`) → adapters (web / video) → a generic full-page translation fallback; selection translation is implemented independently.
- `/v1/chat/completions` exists so that BabelDOC can call this runtime directly as its translation backend — the same seam any other tool can use.
- The service auto-exits after 20 minutes of idle to free memory; the extension relaunches it with one click when needed.
- Detailed design docs (architecture planning, competitor analysis, feature proposals) live in [`docs/`](docs/).

</details>

---

## Contributing

This project is built by one person in spare time — **one person covers a few use cases; a community covers all of them**. Contributions especially welcome in these areas:

- 🔌 **Runtime & API** — more client-facing endpoints, better streaming behavior, integration examples for other tools
- 🌐 **More site adapters** — make the sites you frequent work
- 📄 **PDF polish** — better translation for more layouts (two-column papers / manga / financial reports)
- 🎬 **More video platforms** — subtitle translation for Bilibili, Coursera, Netflix, etc.
- 🖥️ **Platform support** — one-click install scripts for Windows / Linux (currently only macOS is automated)
- 🐛 **Bug reports** — wrong translations, broken layouts, service won't start — please open an Issue

Before submitting a PR, please make sure `server_gguf.py` passes `python -m py_compile` and that extension changes are verified working in `chrome://extensions`. You don't have to write code — telling me which use case you'd like supported is a valuable contribution too.

---

## Acknowledgements & License

- [Tencent Hunyuan HY-MT](https://github.com/Tencent-Hunyuan/HY-MT) · [Hy-MT2 GGUF](https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF) — the translation model
- [llama.cpp](https://github.com/ggerganov/llama.cpp) / llama-cpp-python — local inference engine
- [BabelDOC](https://github.com/funstory-ai/BabelDOC) — PDF layout analysis & bilingual typesetting
- [PyMuPDF](https://pymupdf.readthedocs.io/) — PDF processing; [Flask](https://flask.palletsprojects.com/) — local service

> ⚠️ **Third-party component licenses (important)**: This project's own code is released under MIT, but its runtime dependencies include **AGPL-3.0** strong-copyleft components — [PyMuPDF](https://pymupdf.readthedocs.io/) (PDF parsing) and [BabelDOC](https://github.com/funstory-ai/BabelDOC) (PDF bilingual typesetting). The AGPL terms are **independent of this project's MIT license**: if you modify these components and serve them over a network, or redistribute them in closed source, you must comply with AGPL-3.0 (including the obligation to publish the source of your modified version). For the full list and each component's license, see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

- **This project's code**: [MIT License](LICENSE)
- **Translation model**: [Tencent HY Community License Agreement](MODEL_LICENSE.txt) — commercial use allowed; a separate license from Tencent is required above 100 million monthly active users; **does not apply to the EU / UK / South Korea**; redistribution must include a copy of the agreement and `NOTICE`; prohibited from being used to improve other AI models, for military purposes, or in high-risk automated decision-making, among other restrictions
- **Third-party components**: include AGPL-3.0 and various permissive licenses (MIT / BSD / Apache-2.0) — see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)
- **Disclaimer**: MagicLingua is not affiliated with, endorsed by, or sponsored by Tencent or the Hunyuan team

---

<div align="center">

**Powered by Tencent Hunyuan HY-MT + llama.cpp** 🚀

<sub>Made for personal use · Given to everyone · Free & open source forever</sub>

</div>
