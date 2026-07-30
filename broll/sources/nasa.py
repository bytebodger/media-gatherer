"""NASA image & video library. No key. Science and cosmology beats.

NASA media is generally public domain (per NASA media usage guidelines), with
occasional exceptions for logos/third-party content noted per item.
"""

from __future__ import annotations

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit

SEARCH = "https://images-api.nasa.gov/search"


class NASA(Adapter):
    name = "nasa"
    supports = {"image", "video"}
    query_family = "both"
    needs_key = False
    rate_limit = RateLimit(concurrency=3, min_interval_s=0.2)

    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        data = await self._get_json(client, SEARCH, {"q": q, "media_type": kind})
        items = (((data.get("collection", {}) or {}).get("items", []))
                 if isinstance(data, dict) else [])
        out: list[Asset] = []
        for it in items[:limit]:
            data_list = it.get("data") or [{}]
            meta = data_list[0]
            links = it.get("links") or []
            thumb = next((l.get("href") for l in links if l.get("render") == "image"), None)
            if not thumb and links:
                thumb = links[0].get("href")
            if not thumb:
                continue
            out.append(Asset(
                source=self.name,
                source_id=str(meta.get("nasa_id")),
                kind=kind,
                title=meta.get("title"),
                description=meta.get("description"),
                page_url=f"https://images.nasa.gov/details-{meta.get('nasa_id')}",
                thumb_url=thumb,
                full_url=it.get("href"),  # collection.json manifest; resolved at export
                license_id="pd",
                license_url="https://www.nasa.gov/nasa-brand-center/images-and-media/",
                attribution_required=False,
                creator=meta.get("photographer") or meta.get("secondary_creator"),
                raw=it,
            ))
        return out
