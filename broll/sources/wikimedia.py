"""Wikimedia Commons. No key. Text search is genuinely weak (see caveat below).

Caveat: Commons text search is bad. The better path is resolving an entity to a
Commons *category* and listing its members. That's a separate code path
(`search_category`) and where a lot of the best material lives. Text search here
is the baseline; category traversal is the upgrade.
"""

from __future__ import annotations

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit, normalize_license

API = "https://commons.wikimedia.org/w/api.php"


class Wikimedia(Adapter):
    name = "wikimedia"
    supports = {"image"}
    query_family = "archival"
    needs_key = False
    rate_limit = RateLimit(concurrency=3, min_interval_s=0.2)

    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        if kind != "image":
            return []
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": q,
            "gsrnamespace": "6",  # File:
            "gsrlimit": str(limit),
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|size|mime",
            "iiurlwidth": "640",
        }
        data = await self._get_json(client, API, params)
        pages = (data.get("query", {}) or {}).get("pages", {}) if isinstance(data, dict) else {}
        out: list[Asset] = []
        for page in pages.values():
            info = (page.get("imageinfo") or [{}])[0]
            if not info.get("url"):
                continue
            meta = info.get("extmetadata", {}) or {}
            lic_txt = _meta(meta, "LicenseShortName") or _meta(meta, "License")
            lic_url = _meta(meta, "LicenseUrl")
            lic_id, attr = normalize_license(lic_txt, lic_url)
            out.append(Asset(
                source=self.name,
                source_id=str(page.get("pageid")),
                kind="image",
                title=page.get("title", "").removeprefix("File:"),
                description=_meta(meta, "ImageDescription"),
                page_url=info.get("descriptionurl", ""),
                thumb_url=info.get("thumburl") or info["url"],
                full_url=info.get("url"),
                width=info.get("width"),
                height=info.get("height"),
                mime=info.get("mime"),
                license_id=lic_id,
                license_url=lic_url,
                attribution_required=attr,
                creator=_strip_html(_meta(meta, "Artist")),
                raw=page,
            ))
        return out


def _meta(meta: dict, key: str) -> str | None:
    v = meta.get(key)
    return v.get("value") if isinstance(v, dict) else None


def _strip_html(s: str | None) -> str | None:
    if not s:
        return s
    import re
    return re.sub(r"<[^>]+>", "", s).strip() or None
