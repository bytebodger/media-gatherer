"""Internet Archive. No key. License lives on the ITEM, not the collection.

Roughly a third of Prelinger isn't cleanly public domain. We read `licenseurl`
per item and never infer from collection membership. Items without a clear
license go to quarantine.
"""

from __future__ import annotations

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit, normalize_license

SEARCH = "https://archive.org/advancedsearch.php"


class InternetArchive(Adapter):
    name = "internetarchive"
    supports = {"image", "video"}
    query_family = "archival"
    needs_key = False
    rate_limit = RateLimit(concurrency=2, min_interval_s=0.3)

    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        mediatype = "movies" if kind == "video" else "image"
        params = {
            "q": f'({q}) AND mediatype:{mediatype}',
            "fl[]": ["identifier", "title", "licenseurl", "mediatype", "creator"],
            "rows": str(limit),
            "output": "json",
        }
        data = await self._get_json(client, SEARCH, params)
        docs = ((data.get("response", {}) or {}).get("docs", [])
                if isinstance(data, dict) else [])
        out: list[Asset] = []
        for d in docs:
            ident = d.get("identifier")
            if not ident:
                continue
            lic_url = d.get("licenseurl")
            lic_id, attr = normalize_license(None, lic_url)
            out.append(Asset(
                source=self.name,
                source_id=ident,
                kind=kind,
                title=d.get("title") if isinstance(d.get("title"), str) else ident,
                page_url=f"https://archive.org/details/{ident}",
                thumb_url=f"https://archive.org/services/img/{ident}",
                full_url=f"https://archive.org/download/{ident}",
                license_id=lic_id,
                license_url=lic_url,
                attribution_required=attr,
                creator=d.get("creator") if isinstance(d.get("creator"), str) else None,
                raw=d,
            ))
        return out
