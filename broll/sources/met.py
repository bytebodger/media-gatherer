"""The Met Museum. No key, CC0, clean API. Search returns IDs; we fetch objects."""

from __future__ import annotations

import asyncio

import httpx

from ..models import Asset, Kind
from .base import Adapter, RateLimit

SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1/search"
OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{id}"


class Met(Adapter):
    name = "met"
    supports = {"image"}
    query_family = "archival"
    needs_key = False
    # Each search fans out into up to `limit` object fetches; under the whole
    # pipeline's concurrency the Met 429s past ~a handful/sec, so keep it gentle
    # (backoff in _get_json mops up the occasional 429 that still slips through).
    rate_limit = RateLimit(concurrency=2, min_interval_s=0.15)

    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        if kind != "image":
            return []
        data = await self._get_json(client, SEARCH, {"q": q, "hasImages": "true"})
        ids = (data.get("objectIDs") or [])[:limit] if isinstance(data, dict) else []
        results = await asyncio.gather(
            *(self._object(client, oid) for oid in ids), return_exceptions=True
        )
        return [a for a in results if isinstance(a, Asset)]

    async def _object(self, client: httpx.AsyncClient, oid: int) -> Asset | None:
        obj = await self._get_json(client, OBJECT.format(id=oid))
        if not isinstance(obj, dict) or not obj.get("primaryImageSmall"):
            return None
        cc0 = bool(obj.get("isPublicDomain"))
        return Asset(
            source=self.name,
            source_id=str(oid),
            kind="image",
            title=obj.get("title"),
            description=obj.get("objectDate"),
            page_url=obj.get("objectURL", ""),
            thumb_url=obj.get("primaryImageSmall", ""),
            full_url=obj.get("primaryImage") or obj.get("primaryImageSmall"),
            license_id="cc0" if cc0 else "unknown",
            license_url="https://creativecommons.org/publicdomain/zero/1.0/" if cc0 else None,
            attribution_required=False,
            creator=obj.get("artistDisplayName") or None,
            raw=obj,
        )
