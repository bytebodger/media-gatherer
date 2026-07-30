"""SourceAdapter interface, rate limiting, and license normalization.

Adding a source is one file plus one registry line. That matters because you'll
want to keep adding them for years.
"""

from __future__ import annotations

import abc
import asyncio
import random
import time
from dataclasses import dataclass
from typing import Literal

import httpx

from ..models import Asset, Kind

QueryFamily = Literal["archival", "atmospheric", "both"]

# Sent on every outbound request. Several keyless sources (the Met's WAF,
# Wikimedia's robot policy) 403 a generic or missing User-Agent, so identify the
# client with a contact address per Wikimedia's UA policy (https://w.wiki/4wJS).
USER_AGENT = "broll-prepass/0.1 (+mailto:skepticus.channel@gmail.com)"


@dataclass
class RateLimit:
    concurrency: int = 4
    min_interval_s: float = 0.0  # minimum spacing between calls to this source


class Throttle:
    """Per-adapter concurrency cap + minimum call spacing."""

    def __init__(self, rl: RateLimit):
        self._sem = asyncio.Semaphore(rl.concurrency)
        self._min_interval = rl.min_interval_s
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self):
        await self._sem.acquire()
        if self._min_interval:
            async with self._lock:
                wait = self._min_interval - (time.monotonic() - self._last)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()


# --- license normalization ---------------------------------------------------

_PD_MARKERS = ("public domain", "pdm", "no known copyright", "no known restrictions",
               "cc0", "publicdomain")


def normalize_license(text: str | None, url: str | None = None) -> tuple[str, bool]:
    """Return (license_id, attribution_required). Unknown -> ('unknown', False)."""
    blob = f"{text or ''} {url or ''}".lower()
    if not blob.strip():
        return "unknown", False
    if "cc0" in blob or "publicdomain/zero" in blob:
        return "cc0", False
    if any(m in blob for m in _PD_MARKERS):
        return "pd", False
    # creative commons variants
    for tag in ("by-sa", "by-nc-nd", "by-nc-sa", "by-nc", "by-nd", "by"):
        if f"cc-{tag}" in blob or f"/{tag}/" in blob or f" {tag} " in blob or f"cc {tag}" in blob:
            return f"cc-{tag}", True
    if "creativecommons" in blob or "creative commons" in blob:
        return "cc-by", True
    return "unknown", False


class Adapter(abc.ABC):
    name: str = "base"
    supports: set[Kind] = {"image"}
    query_family: QueryFamily = "both"
    needs_key: bool = False
    attribution_required: bool = False
    rate_limit: RateLimit = RateLimit()

    def __init__(self):
        self._throttle = Throttle(self.rate_limit)

    def wants(self, family: str) -> bool:
        return self.query_family == "both" or self.query_family == family

    @abc.abstractmethod
    async def search(self, client: httpx.AsyncClient, q: str, kind: Kind,
                     limit: int) -> list[Asset]:
        ...

    # helpers -----------------------------------------------------------------
    async def _get_json(self, client: httpx.AsyncClient, url: str,
                        params: dict | None = None, headers: dict | None = None,
                        max_retries: int = 4) -> dict | list:
        """GET + parse JSON, retrying 429/5xx and transient network errors.

        Backoff honors a server ``Retry-After`` when present (LOC and the Met
        send one on 429), else uses capped exponential backoff with jitter. The
        throttle wraps all attempts so retries still respect per-source spacing.
        """
        async with self._throttle:
            r = await get_with_retries(client, url, params=params, headers=headers,
                                       max_retries=max_retries)
            return r.json()


# --- retry helpers -----------------------------------------------------------

_RETRY_STATUS = {429, 500, 502, 503, 504}


async def get_with_retries(client: httpx.AsyncClient, url: str, *,
                           params: dict | None = None, headers: dict | None = None,
                           follow_redirects: bool = False, timeout: float = 30.0,
                           max_retries: int = 4) -> httpx.Response:
    """GET with retries on 429/5xx and transient network errors.

    Backoff honors a server ``Retry-After`` when present (LOC, the Met and
    upload.wikimedia.org all send one on 429), else uses capped exponential
    backoff with jitter. Shared by both the search adapters and the thumbnail
    fetcher so rate-limit handling lives in exactly one place.
    """
    for attempt in range(max_retries + 1):
        try:
            r = await client.get(url, params=params, headers=headers,
                                  timeout=timeout, follow_redirects=follow_redirects)
            r.raise_for_status()
            return r
        except httpx.HTTPStatusError as e:
            # A Cloudflare bot challenge (e.g. LOC) is a JS puzzle a plain
            # client can't solve — retrying only burns time and deepens the
            # block, so fail fast instead of backing off.
            if e.response.headers.get("cf-mitigated") == "challenge":
                raise
            if e.response.status_code not in _RETRY_STATUS or attempt == max_retries:
                raise
            delay = _retry_after(e.response) or _backoff(attempt)
        except httpx.TransportError:  # timeouts, connection resets
            if attempt == max_retries:
                raise
            delay = _backoff(attempt)
        await asyncio.sleep(delay)


def _backoff(attempt: int) -> float:
    """Exponential backoff (0.5, 1, 2, 4 ...) with jitter, capped at 20s."""
    return min(20.0, 0.5 * (2 ** attempt)) + random.uniform(0.0, 0.5)


def _retry_after(response: httpx.Response) -> float | None:
    """Parse a Retry-After header (delta-seconds form), capped at 30s."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(30.0, float(raw))
    except ValueError:
        return None  # HTTP-date form — fall back to exponential backoff
