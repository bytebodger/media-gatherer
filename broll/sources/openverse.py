"""Openverse aggregator. No key required (anonymous rate-limited). Decent fallback."""

from __future__ import annotations

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit, normalize_license

API = "https://api.openverse.org/v1/images/"


class Openverse(Adapter):
    name = "openverse"
    supports = {"image"}
    query_family = "both"
    needs_key = False
    rate_limit = RateLimit(concurrency=2, min_interval_s=0.4)

    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        if kind != "image":
            return []
        params = {"q": q, "page_size": str(min(limit, 20))}
        data = await self._get_json(client, API, params)
        results = data.get("results", []) if isinstance(data, dict) else []
        out: list[Asset] = []
        for r in results[:limit]:
            lic_txt = f"{r.get('license', '')} {r.get('license_version', '')}"
            lic_id, attr = normalize_license(lic_txt, r.get("license_url"))
            out.append(Asset(
                source=self.name,
                source_id=str(r.get("id")),
                kind="image",
                title=r.get("title"),
                page_url=r.get("foreign_landing_url", ""),
                thumb_url=r.get("thumbnail") or r.get("url", ""),
                full_url=r.get("url"),
                width=r.get("width"),
                height=r.get("height"),
                license_id=lic_id,
                license_url=r.get("license_url"),
                attribution_required=attr,
                creator=r.get("creator"),
                creator_url=r.get("creator_url"),
                raw=r,
            ))
        return out
