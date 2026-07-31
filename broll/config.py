"""Configuration. Reads from environment / .env; sensible defaults everywhere."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional at runtime
    pass


def _env(name: str) -> str | None:
    val = os.environ.get(name)
    return val.strip() if val and val.strip() else None


@dataclass
class Config:
    # LLM. Backend chooses HOW stages 1-2 call a model:
    #   "auto" (default) -> your Claude subscription via the Claude Code CLI if
    #                       installed, else offline. NEVER the metered API.
    #   "subscription"   -> force the Claude Code CLI (your account).
    #   "api"            -> opt in to the metered Anthropic API (pay-per-token).
    #   "offline"        -> never call a model; use the rule-based fallback.
    llm_backend: str = field(default_factory=lambda: _env("BROLL_LLM_BACKEND") or "auto")
    anthropic_api_key: str | None = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    llm_model: str = field(default_factory=lambda: _env("BROLL_LLM_MODEL") or "claude-opus-4-8")
    llm_timeout_s: float = 600.0

    # source keys
    pexels_key: str | None = field(default_factory=lambda: _env("PEXELS_API_KEY"))
    pixabay_key: str | None = field(default_factory=lambda: _env("PIXABAY_API_KEY"))
    smithsonian_key: str | None = field(default_factory=lambda: _env("SMITHSONIAN_API_KEY"))
    rijksmuseum_key: str | None = field(default_factory=lambda: _env("RIJKSMUSEUM_API_KEY"))
    dvids_key: str | None = field(default_factory=lambda: _env("DVIDS_API_KEY"))

    # paths
    data_dir: Path = field(default_factory=lambda: Path(_env("BROLL_DATA_DIR") or ".broll"))

    # pipeline knobs
    beats_target: int = 16
    variants_per_family: int = 4
    per_source_query_budget: int = 3  # cap variants fired at any one source
    candidates_per_beat: int = 30
    limit_per_query: int = 15

    # quality gate
    min_width: int = 900  # media narrower than this is quarantined; also the
                          # default for the review page's live min-width filter
    landscape_only: bool = True
    video_min_seconds: float = 4.0
    video_max_seconds: float = 60.0

    # rerank
    use_clip: bool = field(default_factory=lambda: _env("BROLL_USE_CLIP") == "1")
    clip_model: str = "ViT-B-16-SigLIP"
    clip_pretrained: str = "webli"

    # [7] style pre-treatment. Defined once, applied to every exported asset so a
    # 15th-century woodcut and a 1970s photo become the same visual language.
    duotone_shadow: str = "#241a12"     # dark tone (channel colors, hex)
    duotone_highlight: str = "#efe4cd"  # light tone
    grain: int = 8                      # 0 = off
    vignette: float = 0.35              # 0..1
    # ffmpeg video filter chain. Kept editable in one place.
    ffmpeg_vf: str = "format=gray,eq=contrast=1.06:brightness=0.02,noise=alls=6:allf=t,vignette=PI/5"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "broll.db"

    @property
    def thumbs_dir(self) -> Path:
        return self.data_dir / "thumbs"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.thumbs_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    cfg = Config()
    cfg.ensure_dirs()
    return cfg
