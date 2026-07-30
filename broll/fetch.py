"""[4] Two-phase fetch — thumbnails only.

This isn't an optimization, it's a requirement. 12 beats x 30 candidates x a
60MB video file is 20GB for a set you'll discard 95% of. Thumbnails are a few
hundred KB. Full assets download only at export, only for what you selected.
"""

from __future__ import annotations

import asyncio
import io
from pathlib import Path

import httpx

from .config import Config
from .db import Store
from .models import Asset
from .sources.base import USER_AGENT, get_with_retries

try:
    from PIL import Image
    import imagehash
    _HAVE_IMG = True
except Exception:  # Pillow/imagehash optional at import time
    _HAVE_IMG = False


def _thumb_path(cfg: Config, asset: Asset) -> Path:
    safe_id = "".join(c if c.isalnum() else "_" for c in asset.source_id)[:80]
    return cfg.thumbs_dir / f"{asset.source}_{safe_id}.jpg"


def _save_thumb(content: bytes, path: Path) -> str | None:
    """Write a downscaled JPEG thumbnail; return its phash. Raises on non-images."""
    if not _HAVE_IMG:
        path.write_bytes(content)
        return None
    img = Image.open(io.BytesIO(content)).convert("RGB")
    img.thumbnail((640, 640))
    img.save(path, "JPEG", quality=85)
    return str(imagehash.phash(img))


async def _fetch_one(client: httpx.AsyncClient, cfg: Config, store: Store,
                     asset: Asset, sem: asyncio.Semaphore) -> str:
    """Return 'cached' | 'ok' | 'skip' (no url) | 'fail' (download/decode error)."""
    # Corpus reuse: if we already have a thumbnail + phash, skip the download.
    row = store.get_asset_row(asset.source, asset.source_id)
    if row and row["thumb_path"] and Path(row["thumb_path"]).exists():
        store.upsert_asset(asset)  # refresh payload, keep phash/thumb
        return "cached"
    if not asset.thumb_url:
        store.upsert_asset(asset)
        return "skip"
    path = _thumb_path(cfg, asset)
    # Try the thumbnail, then fall back to the full-res URL. Some proxies (notably
    # Openverse) 424 on their thumbnail endpoint while the original still loads,
    # and we downscale to 640 regardless — so the full URL is a fine thumb source.
    candidates = [asset.thumb_url]
    if asset.full_url and asset.full_url != asset.thumb_url:
        candidates.append(asset.full_url)
    for url in candidates:
        try:
            async with sem:
                # Retry 429/5xx: CDNs like upload.wikimedia.org throttle burst
                # downloads, and a swallowed 429 here is exactly what renders a
                # tile black in the review UI.
                r = await get_with_retries(client, url, follow_redirects=True, timeout=30.0)
                content = r.content
            phash = _save_thumb(content, path)
        except Exception:
            continue  # try the next candidate URL
        store.upsert_asset(asset, phash=phash, thumb_path=str(path))
        return "ok"
    store.upsert_asset(asset)  # keep the record even if every URL failed
    return "fail"


async def fetch_thumbs(cfg: Config, store: Store, assets: list[Asset],
                       concurrency: int = 5, log=None) -> dict[str, int]:
    """Download thumbnails for a batch of assets and record phash + path in the DB.

    Returns a tally of outcomes so callers can surface silent failures (a thumb
    that never downloads becomes a blank tile, so those must not pass unnoticed).
    """
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        results = await asyncio.gather(
            *(_fetch_one(client, cfg, store, a, sem) for a in assets),
            return_exceptions=True,
        )
    tally: dict[str, int] = {"ok": 0, "cached": 0, "skip": 0, "fail": 0}
    for r in results:
        tally[r if isinstance(r, str) else "fail"] += 1
    if log and tally["fail"]:
        log(f"    ! {tally['fail']} thumbnails failed to download "
            f"(dead source or unsupported) - those tiles show 'no preview'")
    return tally
