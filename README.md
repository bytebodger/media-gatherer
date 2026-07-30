# B-Roll Pre-Pass

A batch tool that reads a video script, breaks it into visual beats, searches
open-license image and video archives, and dumps a reviewable set of candidates
into a local folder. You run it while you're recording. By the time you sit down
to edit, you're browsing instead of searching.

**The tool never picks the image. It only removes the searching.** It puts ~30
plausible, already-license-cleared options in front of you per beat so your eyes
do the last 5 seconds of work.

See [`broll_prepass_architecture.md`](broll_prepass_architecture.md) for the full design.

## Pipeline

```
script.md → [1] segment → [2] query gen → [3] source adapters → [4] thumb fetch
          → [5] filter+rank → [6] review UI → [7] export
```

Stages 1–5 are the unattended batch (`broll run`). Stage 6 is you (`broll review`).
Stage 7 is a second batch (`broll export`).

## Install

```bash
python -m pip install -e .
# optional CLIP rerank (heavy — pulls torch):
python -m pip install -e ".[clip]"
```

## Configure

Copy `.env.example` to `.env` and fill in whatever keys you have. **Everything is
optional.**

### LLM backend (segmentation + query generation)

By default the app uses **your Claude subscription via the Claude Code CLI** — it
shells out to `claude -p` with the API key stripped from the subprocess, so it
**never bills the metered pay-per-token API**. Just be logged in to Claude Code
(`claude` on your PATH). Check what's active:

```bash
broll llm
```

`BROLL_LLM_BACKEND` controls it: `auto` (default — subscription if the `claude`
CLI is present, else offline), `subscription` (force the CLI), `offline` (never
call a model — rule-based fallback), or `api` (opt in to the metered API; needs
`ANTHROPIC_API_KEY`). The metered API is **never** used unless you set
`BROLL_LLM_BACKEND=api` explicitly.

### Source keys

Missing source keys → those adapters are skipped automatically.

Sources needing **no key**: Wikimedia, Met, Library of Congress, Internet Archive,
NASA, Openverse. Keyed: Pexels, Pixabay, Smithsonian, Rijksmuseum, DVIDS.

```bash
broll sources   # shows which adapters are on with your current keys
```

## Use

```bash
# 1-5: script -> ranked candidates (images only by default; --video adds video)
broll run examples/sample_script.md

# 6: local keyboard-driven review (←→↑↓ move, space select, enter next beat, q quarantine)
broll review

# 7: download full-res selections, apply the style treatment, write attribution.md
broll export
```

Everything lives under `.broll/` (SQLite job store, thumbnail cache, exports).
Override with `BROLL_DATA_DIR`.

## Why SQLite

One file buys three things: **resumability** (a run that dies at beat 9 restarts
at beat 9), **ToS compliance** (Pixabay's 24h response cache for free), and
**cross-video corpus reuse** — `assets` and `embeddings` are global, so over 50
videos you build a private, pre-vetted, license-cleared corpus specific to your
channel's subject matter.

## License gate

Anything with an unknown license goes to a quarantine bucket, **hidden by
default** — not a warning banner. You should never be one careless click away
from an unlicensed asset. Toggle the quarantine pile deliberately in the UI with
`q` if you ever want to look.

## Build order (per the architecture doc)

- **Phase 1** — script → beats → stock → thumbnails → contact sheet.
- **Phase 2** — SQLite, resumability, adapter protocol, archival sources.
- **Phase 3** — video adapters (`--video`).
- **Phase 4** — CLIP rerank (`--clip`, needs the `clip` extra).
- **Phase 5** — export with style pre-treatment + attribution manifest.

All five are implemented here.

## Requirements

Python 3.10+. `ffmpeg` on PATH is used for video frame extraction and the export
treatment chain; without it, video export copies the untreated original.
