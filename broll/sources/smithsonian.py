"""Smithsonian Open Access. Free key required. CC0 open-access media."""

from __future__ import annotations

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit

API = "https://api.si.edu/openaccess/api/v1.0/search"


class Smithsonian(Adapter):
    name = "smithsonian"
    supports = {"image"}
    query_family = "archival"
    needs_key = True
    rate_limit = RateLimit(concurrency=2, min_interval_s=0.3)

    def __init__(self, key: str):
        super().__init__()
        self.key = key

    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        if kind != "image":
            return []
        params = {"api_key": self.key, "q": f"{q} AND online_media_type:Images",
                  "rows": min(limit, 20)}
        data = await self._get_json(client, API, params)
        rows = (((data.get("response", {}) or {}).get("rows", []))
                if isinstance(data, dict) else [])
        out: list[Asset] = []
        for row in rows[:limit]:
            content = row.get("content", {}) or {}
            dnr = content.get("descriptiveNonRepeating", {}) or {}
            media = ((dnr.get("online_media", {}) or {}).get("media", []) or [])
            if not media:
                continue
            m = media[0]
            usage = (m.get("usage", {}) or {}).get("access", "")
            cc0 = usage.upper() == "CC0"
            out.append(Asset(
                source=self.name, source_id=str(row.get("id")), kind="image",
                title=row.get("title"),
                page_url=dnr.get("record_link", ""),
                thumb_url=m.get("thumbnail") or m.get("content", ""),
                full_url=m.get("content"),
                license_id="cc0" if cc0 else "unknown",
                license_url="https://creativecommons.org/publicdomain/zero/1.0/" if cc0 else None,
                attribution_required=False,
                raw=row,
            ))
        return out
