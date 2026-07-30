"""[5] Filter and rank. Cheapest gates first.

Order:
  1. License gate (hard)  -> unknown license => quarantine, hidden by default.
  2. Quality gate         -> min resolution, landscape, video duration bounds.
  3. Dedupe               -> perceptual hash; the same engraving lives in six archives.
  4. CLIP rerank (optional, the actual differentiator).

Without CLIP, ranking falls back to a deterministic score (source weight +
license preference + resolution). CLIP is what turns recall into precision.
"""

from __future__ import annotations

from pathlib import Path

from .config import Config
from .db import Store
from .models import Asset, Beat, Candidate

# Preference order when there's no CLIP signal to separate candidates.
_SOURCE_WEIGHT = {
    "met": 1.0, "rijksmuseum": 1.0, "wikimedia": 0.9, "smithsonian": 0.9,
    "loc": 0.85, "nasa": 0.85, "internetarchive": 0.8, "dvids": 0.8,
    "openverse": 0.7, "pexels": 0.75, "pixabay": 0.7,
}
_LICENSE_BONUS = {"cc0": 0.3, "pd": 0.3, "pexels": 0.2, "pixabay": 0.2}


def _license_gate(asset: Asset) -> str | None:
    if asset.license_id == "unknown":
        return "unknown license"
    return None


def _quality_gate(cfg: Config, asset: Asset) -> str | None:
    w, h = asset.width, asset.height
    if w and h:
        if cfg.landscape_only and w < h:
            return "not landscape"
        if w < cfg.min_width:
            return f"below {cfg.min_width}px"
    if asset.kind == "video" and asset.duration_s:
        if asset.duration_s < cfg.video_min_seconds:
            return "too short"
        if asset.duration_s > cfg.video_max_seconds:
            return "too long"
    return None


def _heuristic_score(asset: Asset) -> float:
    score = _SOURCE_WEIGHT.get(asset.source, 0.5)
    score += _LICENSE_BONUS.get(asset.license_id, 0.0)
    if asset.width:
        score += min(asset.width / 8000.0, 0.25)
    return score


def rank_beat(cfg: Config, store: Store, beat: Beat, assets: list[Asset],
              reranker: "Reranker | None" = None) -> tuple[list[Candidate], list[Candidate]]:
    kept: list[Candidate] = []
    quarantined: list[Candidate] = []
    seen_hashes: list[tuple[int, str]] = []  # (int-hash, key) of kept thumbnails

    for asset in assets:
        reason = _license_gate(asset) or _quality_gate(cfg, asset)
        if reason:
            quarantined.append(Candidate(beat_id=beat.beat_id, asset=asset,
                                         quarantined=True, reject_reason=reason))
            continue

        # dedupe on perceptual hash
        row = store.get_asset_row(asset.source, asset.source_id)
        phash = row["phash"] if row else None
        if phash:
            hv = int(phash, 16)
            dup = next((k for h, k in seen_hashes if _hamming(h, hv) <= 6), None)
            if dup is not None:
                continue  # drop silent duplicate
            seen_hashes.append((hv, f"{asset.source}:{asset.source_id}"))

        kept.append(Candidate(beat_id=beat.beat_id, asset=asset))

    # scoring
    if reranker is not None and reranker.ready:
        reranker.score(store, beat, kept)
    else:
        for c in kept:
            c.score = _heuristic_score(c.asset)

    kept.sort(key=lambda c: c.score, reverse=True)
    kept = kept[: cfg.candidates_per_beat]
    for i, c in enumerate(kept, 1):
        c.rank = i

    return kept, quarantined


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# --- optional CLIP rerank ----------------------------------------------------

class Reranker:
    """SigLIP / large ViT reranker. Embeds visual_intent text and thumbnails,
    sorts by cosine similarity. Embeddings are cached in the DB by (source, id).
    Degrades to a no-op (ready=False) when open_clip/torch aren't installed."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.ready = False
        self._model = None
        self._tokenizer = None
        self._preprocess = None
        self._np = None
        self._torch = None
        if not cfg.use_clip:
            return
        try:
            import numpy as np
            import open_clip
            import torch

            self._np = np
            self._torch = torch
            model, _, preprocess = open_clip.create_model_and_transforms(
                cfg.clip_model, pretrained=cfg.clip_pretrained)
            model.eval()
            self._model = model
            self._preprocess = preprocess
            self._tokenizer = open_clip.get_tokenizer(cfg.clip_model)
            self.ready = True
        except Exception:
            self.ready = False

    def _embed_text(self, text: str):
        tok = self._tokenizer([text])
        with self._torch.no_grad():
            feats = self._model.encode_text(tok)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        return feats[0].cpu().numpy()

    def _embed_image(self, store: Store, asset: Asset):
        cached = store.get_embedding(asset.source, asset.source_id)
        if cached:
            return self._np.frombuffer(cached, dtype="float32")
        row = store.get_asset_row(asset.source, asset.source_id)
        if not row or not row["thumb_path"] or not Path(row["thumb_path"]).exists():
            return None
        from PIL import Image

        img = Image.open(row["thumb_path"]).convert("RGB")
        tensor = self._preprocess(img).unsqueeze(0)
        with self._torch.no_grad():
            feats = self._model.encode_image(tensor)
            feats = feats / feats.norm(dim=-1, keepdim=True)
        vec = feats[0].cpu().numpy().astype("float32")
        store.save_embedding(asset.source, asset.source_id, vec.tobytes())
        return vec

    def score(self, store: Store, beat: Beat, candidates: list[Candidate]) -> None:
        if not self.ready:
            return
        text_vec = self._embed_text(beat.visual_intent or beat.summary)
        for c in candidates:
            img_vec = self._embed_image(store, c.asset)
            if img_vec is None:
                c.score = _heuristic_score(c.asset)
            else:
                c.score = float(self._np.dot(text_vec, img_vec))
