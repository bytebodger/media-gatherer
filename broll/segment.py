"""[1] Segmentation. An LLM pass over the script -> visual beats.

This stage is the whole ballgame. Bad beats produce bad queries and no
downstream cleverness recovers.
"""

from __future__ import annotations

import re

from .config import Config
from .llm import LLMUnavailable, available, parse_into
from .models import Beat, BeatList

SYSTEM = """You break a video script into visual beats for a b-roll search tool.

The tool never picks images — it only removes the searching. Your beats drive
archive queries, so each beat must describe what should be *on screen*.

Rules:
- Produce roughly {target} beats — about one visual change every 60-90 seconds.
- beat_id: sequential, "b_001", "b_002", ...
- char_start / char_end: exact character offsets into the script the beat covers.
  Beats should tile the script in order and not overlap.
- summary: one line, what's being said.
- visual_intent: what should be on screen and why. Be concrete and specific.
- era: the time period if any ("4th century", "1650s", "modern"), else null.
- entities: proper nouns worth searching archives for (people, places, events, works).
- concreteness: 0.0 (abstract, undepictable — route to a text card) to 1.0 (obviously depictable).
  A beat about "the circularity of a transcendental argument" is ~0.1. A beat about
  "the Council of Nicaea" is ~0.9.

Return every beat covering the whole script, in order."""


def _renumber(beats: list[Beat]) -> list[Beat]:
    beats = sorted(beats, key=lambda b: b.char_start)
    for i, b in enumerate(beats, 1):
        b.beat_id = f"b_{i:03d}"
    return beats


def segment(cfg: Config, script: str) -> list[Beat]:
    if available(cfg):
        try:
            user = f"Script ({len(script)} chars):\n\n{script}"
            result = parse_into(cfg, SYSTEM.format(target=cfg.beats_target), user, BeatList)
            beats = [b for b in result.beats if b.char_end > b.char_start]
            if beats:
                return _renumber(beats)
        except LLMUnavailable:
            pass  # fall through to offline
    return segment_offline(cfg, script)


# --- offline fallback --------------------------------------------------------

_ENTITY_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+(?:of|the|de|van)\s+)?(?:[A-Z][a-z]+){0,3})\b")
_ERA_RE = re.compile(
    r"\b(\d{3,4}s?|\d{1,2}(?:st|nd|rd|th)\s+century|(?:early|mid|late)\s+\d{4}s|"
    r"medieval|ancient|renaissance|modern|victorian)\b",
    re.IGNORECASE,
)
_ABSTRACT = {
    "argument", "concept", "theory", "logic", "reason", "meaning", "idea",
    "principle", "assumption", "premise", "epistemology", "metaphysics",
}


def _sentences(text: str) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for m in re.finditer(r"[.!?](?:\s+|$)", text):
        end = m.end()
        if end - start > 1:
            spans.append((start, end))
        start = end
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def segment_offline(cfg: Config, script: str) -> list[Beat]:
    """Deterministic rule-based segmenter. Lower quality, zero dependencies."""
    spans = _sentences(script)
    if not spans:
        return []
    target = max(1, min(cfg.beats_target, len(spans)))
    per = max(1, len(spans) // target)

    beats: list[Beat] = []
    for i in range(0, len(spans), per):
        group = spans[i : i + per]
        cs, ce = group[0][0], group[-1][1]
        text = script[cs:ce].strip()
        if not text:
            continue
        entities = _dedupe(_ENTITY_RE.findall(text))[:5]
        era_m = _ERA_RE.search(text)
        lowered = text.lower()
        abstract_hits = sum(1 for w in _ABSTRACT if w in lowered)
        concreteness = max(0.1, min(1.0, 0.7 + 0.1 * len(entities) - 0.25 * abstract_hits))
        summary = (text[:120] + "…") if len(text) > 120 else text
        beats.append(
            Beat(
                beat_id="b_000",
                char_start=cs,
                char_end=ce,
                summary=summary,
                visual_intent=(
                    f"Imagery evoking: {', '.join(entities)}" if entities
                    else "Atmospheric imagery matching the narration"
                ),
                era=era_m.group(0) if era_m else None,
                entities=entities,
                concreteness=round(concreteness, 2),
            )
        )
    return _renumber(beats)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for it in items:
        it = it.strip()
        key = it.lower()
        if it and key not in seen and len(it) > 2:
            seen.add(key)
            out.append(it)
    return out
