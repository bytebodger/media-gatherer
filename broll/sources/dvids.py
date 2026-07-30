"""DVIDS (Defense Visual Information Distribution Service). Free key required.

Modern, public-domain U.S. military imagery and video (works of the U.S.
federal government). Good for modern-era beats.
"""

from __future__ import annotations

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit

API = "https://api.dvidshub.net/search"


class DVIDS(Adapter):
    name = "dvids"
    supports = {"image", "video"}
    query_family = "archival"
    needs_key = True
    rate_limit = RateLimit(concurrency=2, min_interval_s=0.4)

    def __init__(self, key: str):
        super().__init__()
        self.key = key

    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        params = {"api_key": self.key, "q": q, "type": kind, "max_results": min(limit, 20)}
        data = await self._get_json(client, API, params)
        results = data.get("results", []) if isinstance(data, dict) else []
        out: list[Asset] = []
        for r in results[:limit]:
            thumb = r.get("thumbnail") or r.get("image")
            if not thumb:
                continue
            out.append(Asset(
                source=self.name, source_id=str(r.get("id")), kind=kind,
                title=r.get("title"),
                description=r.get("description"),
                page_url=r.get("url", ""),
                thumb_url=thumb,
                full_url=r.get("image") or r.get("video"),
                duration_s=float(r.get("duration") or 0) or None if kind == "video" else None,
                license_id="pd",
                license_url="https://www.dvidshub.net/about",
                attribution_required=False,
                creator=r.get("credit"),
                raw=r,
            ))
        return out
