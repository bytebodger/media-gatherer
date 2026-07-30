"""Pixabay. Key required. Atmospheric. ~100/60s. ToS: cache responses 24h.

The DB query cache satisfies the 24h caching requirement for free.
"""

from __future__ import annotations

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit

PHOTO = "https://pixabay.com/api/"
VIDEO = "https://pixabay.com/api/videos/"


class Pixabay(Adapter):
    name = "pixabay"
    supports = {"image", "video"}
    query_family = "atmospheric"
    needs_key = True
    rate_limit = RateLimit(concurrency=2, min_interval_s=0.7)  # ~100/60s

    def __init__(self, key: str):
        super().__init__()
        self.key = key

    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        if kind == "video":
            data = await self._get_json(client, VIDEO, {
                "key": self.key, "q": q, "per_page": max(3, limit)})
            return [self._video(v) for v in (data.get("hits", []) if isinstance(data, dict) else [])]
        data = await self._get_json(client, PHOTO, {
            "key": self.key, "q": q, "image_type": "photo",
            "orientation": "horizontal", "per_page": max(3, limit)})
        return [self._photo(p) for p in (data.get("hits", []) if isinstance(data, dict) else [])]

    def _photo(self, p: dict) -> Asset:
        return Asset(
            source=self.name, source_id=str(p.get("id")), kind="image",
            page_url=p.get("pageURL", ""),
            thumb_url=p.get("webformatURL", ""),
            full_url=p.get("largeImageURL") or p.get("fullHDURL") or p.get("imageURL"),
            width=p.get("imageWidth"), height=p.get("imageHeight"),
            license_id="pixabay",
            license_url="https://pixabay.com/service/license-summary/",
            attribution_required=False,
            creator=p.get("user"),
            raw=p,
        )

    def _video(self, v: dict) -> Asset:
        streams = v.get("videos", {})
        best = streams.get("large") or streams.get("medium") or {}
        pid = v.get("picture_id") or v.get("id")
        return Asset(
            source=self.name, source_id=str(v.get("id")), kind="video",
            page_url=v.get("pageURL", ""),
            thumb_url=f"https://i.vimeocdn.com/video/{pid}_640x360.jpg" if pid else "",
            full_url=best.get("url"),
            width=best.get("width"), height=best.get("height"),
            duration_s=float(v.get("duration") or 0) or None,
            license_id="pixabay",
            license_url="https://pixabay.com/service/license-summary/",
            attribution_required=False,
            creator=v.get("user"),
            raw=v,
        )
