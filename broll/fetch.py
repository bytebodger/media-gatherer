"""[4] Two-phase fetch — thumbnails only.

This isn't an optimization, it's a requirement. 12 beats x 30 candidates x a
60MB video file is 20GB for a set you'll discard 95% of. Thumbnails are a few
hundred KB. Full assets download only at export, only for what you selected.
"""

from __future__ import annotations

import asyncio
import io
import warnings
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import Config
from .db import Store
from .models import Asset
from .sources.base import USER_AGENT, get_with_retries

try:
    from PIL import Image, ImageFile
    import imagehash
    _HAVE_IMG = True
except Exception:  # Pillow/imagehash optional at import time
    _HAVE_IMG = False


def _thumb_path(cfg: Config, asset: Asset) -> Path:
    safe_id = "".join(c if c.isalnum() else "_" for c in asset.source_id)[:80]
    return cfg.thumbs_dir / f"{asset.source}_{safe_id}.jpg"


async def _resolve_image_url(client: httpx.AsyncClient, asset: Asset) -> str | None:
    """A direct image URL whose header we can read to measure original dimensions.

    Some sources don't hand back a direct file: Internet Archive's full_url is a
    download *directory* and NASA's is a renditions manifest, so resolve those to
    the largest actual image first (mirrors export's URL resolution).
    """
    if asset.source == "internetarchive":
        try:
            r = await get_with_retries(
                client, f"https://archive.org/metadata/{asset.source_id}", timeout=30.0)
            files = r.json().get("files", []) if isinstance(r.json(), dict) else []
            picks = [f for f in files if str(f.get("name", "")).lower().endswith(
                (".jpg", ".jpeg", ".png", ".tif", ".tiff"))]
            picks.sort(key=lambda f: int(f.get("size", 0) or 0), reverse=True)
            if picks:
                return (f"https://archive.org/download/{asset.source_id}/"
                        f"{quote(picks[0]['name'])}")
        except Exception:
            pass
        return asset.thumb_url or None
    if asset.source == "nasa" and asset.full_url and asset.full_url.endswith(".json"):
        try:
            r = await get_with_retries(client, asset.full_url, timeout=30.0)
            items = r.json()
            if isinstance(items, list):
                orig = [u for u in items if isinstance(u, str) and "orig" in u.lower()]
                cand = orig or [u for u in items if isinstance(u, str)
                                and u.lower().endswith((".jpg", ".jpeg", ".png"))]
                if cand:
                    return cand[0]
        except Exception:
            pass
        return asset.thumb_url or None
    return asset.full_url or asset.thumb_url or None


async def _probe_dimensions(client: httpx.AsyncClient, asset: Asset,
                            sem: asyncio.Semaphore) -> tuple[int, int] | None:
    """Read just enough of the original image to learn its pixel dimensions.

    Streams bytes into an incremental parser and stops the moment the size is
    known (a JPEG/PNG header is a few KB), so we never download the full file.
    """
    if not _HAVE_IMG:
        return None
    url = await _resolve_image_url(client, asset)
    if not url:
        return None
    try:
        async with sem:
            # Partial reads of big/odd scans trip PIL warnings (truncated file,
            # decompression-bomb size, corrupt EXIF) — all harmless here since we
            # only read the header for its size, never decode. Keep them quiet.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                parser = ImageFile.Parser()
                read = 0
                async with client.stream("GET", url, timeout=30.0,
                                         follow_redirects=True) as r:
                    r.raise_for_status()
                    async for chunk in r.aiter_bytes(8192):
                        parser.feed(chunk)
                        read += len(chunk)
                        if parser.image is not None:
                            return parser.image.size
                        if read > 3_000_000:  # give up rather than pull a whole file
                            break
    except Exception:
        return None
    return None


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
    # Original dimensions: adapters that report them (wikimedia, openverse) win;
    # otherwise reuse a previously-probed value, else measure the original image.
    if asset.width is None or asset.height is None:
        existing = store.get_asset(asset.source, asset.source_id)
        if existing and existing.width and existing.height:
            asset.width, asset.height = existing.width, existing.height
        else:
            dims = await _probe_dimensions(client, asset, sem)
            if dims:
                asset.width, asset.height = dims
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
