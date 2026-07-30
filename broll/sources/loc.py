"""Library of Congress. No key, deep holdings, good JSON API.

Rights vary per item; we only clear items whose rights advisory says so and
quarantine the rest.
"""

from __future__ import annotations

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit, normalize_license

SEARCH = "https://www.loc.gov/photos/"


class LOC(Adapter):
    name = "loc"
    supports = {"image"}
    query_family = "archival"
    needs_key = False
    # LOC caps its search endpoint near 20 req/min and hard-blocks bursts, so
    # go single-file with ~3s spacing (backoff in _get_json covers the rest).
    rate_limit = RateLimit(concurrency=1, min_interval_s=3.0)

    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        if kind != "image":
            return []
        params = {"q": q, "fo": "json", "c": str(limit)}
        data = await self._get_json(client, SEARCH, params)
        results = data.get("results", []) if isinstance(data, dict) else []
        out: list[Asset] = []
        for item in results[:limit]:
            images = item.get("image_url") or []
            if not images:
                continue
            rights = " ".join(item.get("rights_advisory") or []) or item.get("rights", "")
            lic_id, attr = normalize_license(rights)
            out.append(Asset(
                source=self.name,
                source_id=str(item.get("id") or item.get("number") or images[-1]),
                kind="image",
                title=item.get("title"),
                description=rights or None,
                page_url=item.get("url", ""),
                thumb_url=images[0] if images[0].startswith("http") else f"https:{images[0]}",
                full_url=(images[-1] if images[-1].startswith("http") else f"https:{images[-1]}"),
                license_id=lic_id,
                license_url=None,
                attribution_required=attr,
                raw=item,
            ))
        return out
