# B-Roll Pre-Pass: Architecture Sketch

A batch tool that reads a video script, breaks it into visual beats, searches open-license image and video archives, and dumps a reviewable set of candidates into a local folder. You run it while you're recording. By the time you sit down to edit, you're browsing instead of searching.

---

## Core principle

The tool never picks the image. It only removes the searching.

Every design decision below serves that. The goal isn't a smart tool that guesses right. It's a dumb tool that puts 30 plausible options in front of you per beat, already license-cleared, so your eyes do the last 5 seconds of work.

---

## Pipeline

```
script.md
   |
   v
[1] Segment          ->  beats (id, span, summary, visual_intent, era, entities)
   |
   v
[2] Query gen        ->  per beat: archival queries + atmospheric queries
   |
   v
[3] Source adapters  ->  normalized Asset records (parallel, rate-limited)
   |
   v
[4] Thumb fetch      ->  local thumbnail cache (cheap, no full assets yet)
   |
   v
[5] Filter + rank    ->  license gate, quality gate, dedupe, CLIP rerank
   |
   v
[6] Review UI        ->  local web page, contact sheet per beat
   |
   v
[7] Export           ->  full-res download, style pre-treatment, attribution manifest
```

Stages 1 through 5 are the unattended batch. Stage 6 is you. Stage 7 is a second batch.

---

## [1] Segmentation

An LLM pass over the script. Chunk it so you're not fighting context limits on a 5,000 word script.

Output per beat:

```python
class Beat(BaseModel):
    beat_id: str            # "b_007"
    char_start: int         # offsets back into the script
    char_end: int
    summary: str            # one line, what's being said
    visual_intent: str      # what should be on screen and why
    era: str | None         # "4th century", "1650s", "modern", None
    entities: list[str]     # ["Council of Nicaea", "Athanasius", "Arius"]
    concreteness: float     # 0.0 abstract, 1.0 depictable
```

`concreteness` matters more than it looks. A beat about "the transcendental argument's circularity" has no image. Flag it low and route it to a text card instead of burning 40 API calls on it.

**This stage is the whole ballgame.** Bad beats produce bad queries and no amount of downstream cleverness recovers. Budget your iteration time here.

Target roughly 12 to 20 beats per script. That's about one visual change every 60 to 90 seconds.

---

## [2] Query generation

The important insight: different archives want different query languages.

Generate **two query families** per beat and route them separately.

**Archival queries** go to Wikimedia, Met, LOC, Smithsonian, Internet Archive, Rijksmuseum. These want proper nouns, eras, and medium terms.

```
"Council of Nicaea engraving"
"Athanasius of Alexandria icon"
"Byzantine manuscript illumination 9th century"
```

**Atmospheric queries** go to Pexels, Pixabay. These want mood nouns. Proper nouns return nothing.

```
"candlelight old book"
"stone cathedral interior"
"parchment texture close up"
```

Feeding "Council of Nicaea" to Pexels wastes a call. Feeding "moody candlelight" to the Met wastes a call. Route by family, not by beat.

Generate 3 to 5 variants per family. Fan them out.

---

## [3] Source adapters

One file per source. All of them implement the same interface and register their capabilities.

```python
class SourceAdapter(Protocol):
    name: str
    supports: set[Literal["image", "video"]]
    query_family: Literal["archival", "atmospheric", "both"]
    needs_key: bool
    rate_limit: RateLimit
    attribution_required: bool

    async def search(self, q: str, kind: str, limit: int) -> list[Asset]: ...
```

Adding a source becomes one file plus one registry line. That matters because you'll want to keep adding them for years.

### Adapter roster

| Source | Image | Video | Family | Key | Notes |
|---|---|---|---|---|---|
| Pexels | yes | yes | atmospheric | yes | 200/hr, 20k/mo |
| Pixabay | yes | yes | atmospheric | yes | ~100/60s, must cache 24h |
| Wikimedia Commons | yes | thin | archival | no | search is weak, see below |
| Met Museum | yes | no | archival | no | CC0, clean API |
| Smithsonian | yes | some | archival | yes | free key |
| Library of Congress | yes | yes | archival | no | good API, deep holdings |
| Internet Archive | yes | yes | archival | no | per-item license check required |
| NASA | yes | yes | both | no | science and cosmology beats |
| DVIDS | yes | yes | archival | yes | modern, public domain |
| Rijksmuseum | yes | no | archival | yes | free key |
| Openverse | yes | no | both | no | aggregator, decent fallback |

**Wikimedia caveat.** Its text search is genuinely bad. Better results come from resolving your entity to a Commons category and listing the category's members. Build that as a separate code path, not as a query string. It's more work but it's where a lot of your best material lives.

**Internet Archive caveat.** License lives on the item, not the collection. Roughly a third of Prelinger isn't cleanly public domain. Read `licenseurl` and `rights` per item. Never infer from collection membership.

---

## The Asset record

The single most important abstraction. Everything normalizes to this.

```python
class Asset(BaseModel):
    # identity
    source: str
    source_id: str
    kind: Literal["image", "video"]

    # display
    title: str | None
    description: str | None
    page_url: str
    thumb_url: str
    full_url: str | None      # sometimes needs a second call

    # technical
    width: int | None
    height: int | None
    duration_s: float | None  # video only
    file_size: int | None
    mime: str | None

    # licensing - all of this is mandatory
    license_id: str           # "cc0", "pd", "cc-by-4.0", "pexels", "unknown"
    license_url: str | None
    attribution_required: bool
    creator: str | None
    creator_url: str | None

    # provenance
    raw: dict                 # keep the original JSON, always
```

Keep `raw`. Every adapter will surprise you six months from now and you'll want the original payload.

---

## [4] Two-phase fetch

The batch pass downloads **thumbnails only**.

This isn't an optimization, it's a requirement. Twelve beats times 30 candidates times a 60MB video file is 20GB of downloads for a set you'll discard 95% of. Thumbnails are a few hundred KB.

For video, the source usually gives you a poster frame. If it doesn't, pull a single frame with ffmpeg using a range request against the first few seconds. Don't download the file.

Full assets download only at export, only for what you selected.

---

## [5] Filter and rank

Run these in order, cheapest first.

**License gate (hard).** Anything with `license_id == "unknown"` goes to a quarantine bucket, hidden by default. Not a warning banner. Hidden. You should never be one careless click away from an unlicensed asset.

**Quality gate.** Minimum resolution (2560 wide is reasonable if you're compositing behind yourself on a 4K timeline). Landscape only. Video needs a duration floor of maybe 4 seconds and a maximum you're willing to loop.

**Dedupe.** Perceptual hash on thumbnails. The same public domain engraving lives in six archives. You want to see it once.

**CLIP rerank (optional, and the actual differentiator).** Embed the beat's `visual_intent` text. Embed every candidate thumbnail. Sort by cosine similarity.

This is what makes the tool better than Google image search. Keyword search gets you recall. The rerank gets you precision. You have a 4070 sitting there, and scoring a few hundred thumbnails is seconds of work.

For video, embed 3 sampled frames and take the max.

Use `open_clip` with SigLIP or a large ViT. Cache embeddings in the DB keyed by `(source, source_id)`.

---

## Job store

SQLite. One file. This buys you three things at once.

**Resumability.** Checkpoint at `(beat_id, source, query)` granularity. A run that dies at beat 9 restarts at beat 9. Per your standing convention, anything over 30 minutes needs this, and a full 20-beat fan-out across 11 sources will exceed that.

**ToS compliance.** Pixabay asks you to cache responses for 24 hours. The DB does this for free.

**Cross-video reuse.** This one compounds. If you searched "candlelight old book" for a video in March, the results are still there. Over 50 videos you build a private, pre-vetted, license-cleared corpus of material that's specific to your channel's subject matter. That's an asset no stock service sells you.

Tables:

```
jobs(job_id, script_path, created_at, status)
beats(job_id, beat_id, ...)
queries(job_id, beat_id, source, query, family, status, fetched_at)
assets(source, source_id, ... )              -- global, not per-job
candidates(job_id, beat_id, source, source_id, score, rank)
selections(job_id, beat_id, source, source_id, selected_at)
embeddings(source, source_id, vec BLOB)
```

Note that `assets` and `embeddings` are global. That's deliberate. That's the corpus.

---

## [6] Review UI

Local FastAPI serving one static page. Nothing fancy.

Layout: one row per beat. The beat's script text on the left so you know what you're picking for. Contact sheet of ranked candidates on the right.

Requirements that actually matter:

- **Keyboard driven.** Arrow keys to move, space to select, enter to advance. Twelve beats times 30 candidates is 360 decisions. Mouse-only makes it a chore.
- **Video previews play on hover.** Muted, looped, the cached thumbnail until hover.
- **License badge on every tile.** Visible, not on hover. Color-coded by whether attribution is needed.
- **A quarantine toggle** so you can look at the unknown-license pile deliberately, if you ever want to.

Writes `selections.json`. That's the only output.

---

## [7] Export

Second batch pass. Reads `selections.json`.

Downloads full-res. Names files so they sort into your timeline order:

```
beat_003_wikimedia_Council_of_Trent.jpg
beat_007_pexels_2841923.mp4
```

**Style pre-treatment.** This is where you solve the consistency problem, and it belongs here rather than in your editor.

Run each asset through a fixed ffmpeg or Pillow chain. Duotone in your channel colors. Grain. Vignette. Same treatment every time, defined once in a config file.

A 15th century woodcut and a 1970s photograph become the same visual language. You do zero per-image work in the NLE. Documentary editors have unified mismatched archival material this way forever.

```
ffmpeg -i in.mp4 -vf "format=gray,lut3d=skepticus_duotone.cube,noise=alls=6:allf=t" out.mp4
```

Keep the untreated original alongside the treated version. You'll want it eventually.

**Attribution manifest.** Writes `attribution.md` with every asset requiring credit, formatted for your end card. This is the thing you'd otherwise forget and it's the only reason CC-BY material is annoying. Automate it once and CC-BY becomes as easy as CC0.

---

## Build order

**Phase 1 (a weekend).** Script to beats to Pexels and Pixabay to thumbnails to a static HTML contact sheet. No database. No CLIP. No video. Just prove the segmentation produces beats you'd actually want images for. If it doesn't, nothing else matters.

**Phase 2.** SQLite, resumability, the adapter protocol, and the archival sources. This is where it starts beating your current workflow.

**Phase 3.** Video adapters. Deliberately last, because video is a weaker fit for your subject matter and you'll want the image path solid first.

**Phase 4.** CLIP rerank.

**Phase 5.** Export with style pre-treatment and the attribution manifest.

---

## Stack

- Python, since that's your default
- `httpx` async for the fan-out, with a semaphore per adapter for rate limiting
- `pydantic` for the Asset model
- `sqlite3` direct, or `sqlmodel` if you want ORM
- `ffmpeg` for video frames and export treatment
- `Pillow` + `imagehash` for dedupe
- `open_clip_torch` for rerank
- `fastapi` + `uvicorn` for the review page

---

## Honest gaps

**Segmentation quality is the ceiling.** Everything downstream is bounded by whether the LLM understood what the beat is about visually. Expect to iterate on that prompt more than on all the other code combined.

**Wikimedia is the best source and the worst API.** Category traversal helps but it's real work and it never fully solves the problem.

**Video coverage for your niche is thin and no architecture fixes that.** Film starts around 1890. Most of what you talk about is older. Plan for stills as the workhorse.

**Rate limits will bite on wide fan-outs.** 20 beats times 5 query variants times 11 sources is over 1,000 calls. Pexels alone caps at 200 an hour. The DB cache absorbs this over time, but early runs will crawl. Consider a per-source query budget rather than firing every variant at every source.

**Attribution creates an ongoing obligation.** CC-BY material means keeping a credits card current. The manifest automates the tracking but you still have to put it on screen.
