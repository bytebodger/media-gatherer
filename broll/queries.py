"""[2] Query generation. Two query families per beat, routed separately.

Archival queries -> Wikimedia, Met, LOC, etc. Want proper nouns, eras, mediums.
Atmospheric queries -> Pexels, Pixabay. Want mood nouns. Proper nouns return nothing.
"""

from __future__ import annotations

from .config import Config
from .llm import LLMUnavailable, available, parse_into
from .models import Beat, BeatQueries, QueryPlan

SYSTEM = """You write archive search queries for b-roll, given visual beats.

Different archives want different query languages. Produce TWO families per beat:

ARCHIVAL queries go to museum/library archives (Wikimedia, Met, LOC, Smithsonian,
Internet Archive, Rijksmuseum). These want proper nouns, eras, and medium terms:
  "Council of Nicaea engraving"
  "Athanasius of Alexandria icon"
  "Byzantine manuscript illumination 9th century"

ATMOSPHERIC queries go to stock (Pexels, Pixabay). These want mood/scene nouns.
Proper nouns return nothing here:
  "candlelight old book"
  "stone cathedral interior"
  "parchment texture close up"

Rules:
- Generate {n} variants per family per beat.
- For very abstract beats (low concreteness) lean atmospheric; proper nouns won't help.
- Keep each query short (2-6 words). No punctuation, no boolean operators.
- Return archival + atmospheric lists for every beat_id given."""


def generate(cfg: Config, beats: list[Beat]) -> dict[str, BeatQueries]:
    if available(cfg):
        try:
            payload = "\n".join(
                f"{b.beat_id} | intent: {b.visual_intent} | era: {b.era or 'n/a'} "
                f"| entities: {', '.join(b.entities) or 'none'} "
                f"| concreteness: {b.concreteness}"
                for b in beats
            )
            plan = parse_into(
                cfg,
                SYSTEM.format(n=cfg.variants_per_family),
                f"Beats:\n{payload}",
                QueryPlan,
            )
            by_beat = {bq.beat_id: bq for bq in plan.beats}
            # backfill any beat the model skipped
            for b in beats:
                by_beat.setdefault(b.beat_id, _offline_for_beat(cfg, b))
            return by_beat
        except LLMUnavailable:
            pass
    return {b.beat_id: _offline_for_beat(cfg, b) for b in beats}


# --- offline fallback --------------------------------------------------------

_MEDIUMS = ["engraving", "painting", "photograph", "illustration", "map"]
_MOODS = [
    "dramatic light", "close up texture", "atmospheric interior",
    "moody landscape", "vintage documentary",
]


def _offline_for_beat(cfg: Config, beat: Beat) -> BeatQueries:
    n = cfg.variants_per_family
    archival: list[str] = []
    era = beat.era or ""
    for ent in beat.entities[:n]:
        medium = _MEDIUMS[len(archival) % len(_MEDIUMS)]
        archival.append(" ".join(x for x in (ent, era, medium) if x).strip())
    while len(archival) < n and beat.entities:
        ent = beat.entities[len(archival) % len(beat.entities)]
        archival.append(f"{ent} {_MEDIUMS[len(archival) % len(_MEDIUMS)]}".strip())

    # atmospheric: pull evocative nouns from the summary, pad with generic moods
    words = [w.strip(".,;:").lower() for w in beat.summary.split()
             if len(w) > 4 and w[0].islower()]
    atmospheric: list[str] = []
    for i in range(n):
        base = words[i] if i < len(words) else ""
        mood = _MOODS[i % len(_MOODS)]
        atmospheric.append((f"{base} {mood}" if base else mood).strip())

    return BeatQueries(
        beat_id=beat.beat_id,
        archival=_dedupe(archival)[:n],
        atmospheric=_dedupe(atmospheric)[:n],
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for it in items:
        key = it.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append(it.strip())
    return out
