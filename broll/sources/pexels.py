"""Pexels. Key required. Atmospheric. 200/hr, 20k/mo. Images + video."""

from __future__ import annotations

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit

PHOTO = "https://api.pexels.com/v1/search"
VIDEO = "https://api.pexels.com/videos/search"


class Pexels(Adapter):
    name = "pexels"
    supports = {"image", "video"}
    query_family = "atmospheric"
    needs_key = True
    rate_limit = RateLimit(concurrency=2, min_interval_s=0.3)

    def __init__(self, key: str):
        super().__init__()
        self.key = key

    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        headers = {"Authorization": self.key}
        if kind == "video":
            data = await self._get_json(client, VIDEO, {"query": q, "per_page": limit,
                                                        "orientation": "landscape"}, headers)
            return [self._video(v) for v in (data.get("videos", []) if isinstance(data, dict) else [])]
        data = await self._get_json(client, PHOTO, {"query": q, "per_page": limit,
                                                    "orientation": "landscape"}, headers)
        return [self._photo(p) for p in (data.get("photos", []) if isinstance(data, dict) else [])]

    def _photo(self, p: dict) -> Asset:
        src = p.get("src", {})
        return Asset(
            source=self.name, source_id=str(p.get("id")), kind="image",
            title=p.get("alt") or None,
            page_url=p.get("url", ""),
            thumb_url=src.get("large") or src.get("medium", ""),
            full_url=src.get("original"),
            width=p.get("width"), height=p.get("height"),
            license_id="pexels",
            license_url="https://www.pexels.com/license/",
            attribution_required=False,
            creator=p.get("photographer"), creator_url=p.get("photographer_url"),
            raw=p,
        )

    def _video(self, v: dict) -> Asset:
        files = sorted(v.get("video_files", []), key=lambda f: f.get("width") or 0, reverse=True)
        best = files[0] if files else {}
        pics = v.get("video_pictures", [])
        user = v.get("user", {})
        return Asset(
            source=self.name, source_id=str(v.get("id")), kind="video",
            page_url=v.get("url", ""),
            thumb_url=v.get("image") or (pics[0].get("picture") if pics else ""),
            full_url=best.get("link"),
            width=best.get("width") or v.get("width"),
            height=best.get("height") or v.get("height"),
            duration_s=float(v.get("duration") or 0) or None,
            mime=best.get("file_type"),
            license_id="pexels",
            license_url="https://www.pexels.com/license/",
            attribution_required=False,
            creator=user.get("name"), creator_url=user.get("url"),
            raw=v,
        )
