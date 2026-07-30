"""Rijksmuseum. Free key required. CC0 collection imagery."""

from __future__ import annotations

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit

API = "https://www.rijksmuseum.nl/api/en/collection"


class Rijksmuseum(Adapter):
    name = "rijksmuseum"
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
        params = {"key": self.key, "q": q, "imgonly": "true", "ps": min(limit, 20),
                  "format": "json"}
        data = await self._get_json(client, API, params)
        objs = data.get("artObjects", []) if isinstance(data, dict) else []
        out: list[Asset] = []
        for o in objs[:limit]:
            img = o.get("webImage") or {}
            if not img.get("url"):
                continue
            out.append(Asset(
                source=self.name, source_id=str(o.get("objectNumber")), kind="image",
                title=o.get("title"),
                page_url=(o.get("links", {}) or {}).get("web", ""),
                thumb_url=img.get("url", ""),
                full_url=img.get("url"),
                width=img.get("width"), height=img.get("height"),
                license_id="cc0",
                license_url="https://creativecommons.org/publicdomain/zero/1.0/",
                attribution_required=False,
                creator=o.get("principalOrFirstMaker"),
                raw=o,
            ))
        return out
