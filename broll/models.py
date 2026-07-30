"""Core data models. Everything downstream normalizes to Beat and Asset."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["image", "video"]
QueryFamily = Literal["archival", "atmospheric", "both"]


class Beat(BaseModel):
    """One visual beat of the script. Stage [1] output."""

    beat_id: str  # "b_007"
    char_start: int  # offsets back into the script
    char_end: int
    summary: str  # one line, what's being said
    visual_intent: str  # what should be on screen and why
    era: str | None = None  # "4th century", "1650s", "modern", None
    entities: list[str] = Field(default_factory=list)
    concreteness: float = 0.5  # 0.0 abstract, 1.0 depictable


class Query(BaseModel):
    """A single generated query, routed to a source family. Stage [2] output."""

    beat_id: str
    text: str
    family: QueryFamily


class BeatQueries(BaseModel):
    """Two query families for one beat."""

    beat_id: str
    archival: list[str] = Field(default_factory=list)
    atmospheric: list[str] = Field(default_factory=list)


class Asset(BaseModel):
    """The single most important abstraction. Every adapter normalizes to this."""

    # identity
    source: str
    source_id: str
    kind: Kind

    # display
    title: str | None = None
    description: str | None = None
    page_url: str = ""
    thumb_url: str = ""
    full_url: str | None = None  # sometimes needs a second call

    # technical
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None  # video only
    file_size: int | None = None
    mime: str | None = None

    # licensing - all of this is mandatory
    license_id: str = "unknown"  # "cc0", "pd", "cc-by-4.0", "pexels", "unknown"
    license_url: str | None = None
    attribution_required: bool = False
    creator: str | None = None
    creator_url: str | None = None

    # provenance
    raw: dict = Field(default_factory=dict)  # keep the original JSON, always

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.source_id)


class Candidate(BaseModel):
    """An asset scored against a beat. Stage [5] output."""

    beat_id: str
    asset: Asset
    score: float = 0.0
    rank: int = 0
    quarantined: bool = False  # unknown-license or failed a gate, hidden by default
    reject_reason: str | None = None


# Segmentation / query generation are LLM structured-output targets.
class BeatList(BaseModel):
    beats: list[Beat]


class QueryPlan(BaseModel):
    beats: list[BeatQueries]
