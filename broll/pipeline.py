"""Batch pipeline: script -> beats -> queries -> assets -> thumbs -> ranked candidates.

Stages 1-5 run unattended here. Stage 6 (review) and 7 (export) are separate.

Resumable at (beat, source, query) granularity: re-running the same job_id
skips segmentation/query-gen if already done and only executes pending queries.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx

from .config import Config
from .db import Store
from .fetch import fetch_thumbs
from .models import Asset, Beat
from .queries import generate
from .rank import Reranker, rank_beat
from .segment import segment
from .sources import build_adapters
from .sources.base import Adapter, USER_AGENT

_PIXABAY_CACHE_S = 24 * 3600  # ToS: cache Pixabay responses 24h


def new_job_id(script_path: str) -> str:
    stem = Path(script_path).stem[:24].replace(" ", "_")
    return f"{stem}_{int(time.time())}"


def _route_queries(cfg: Config, adapters: list[Adapter],
                   archival: list[str], atmospheric: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Map adapter name -> [(query, family)], respecting family routing + budget.

    Route by family, not by beat. Feeding a proper noun to Pexels wastes a call;
    feeding a mood noun to the Met wastes a call.
    """
    budget = cfg.per_source_query_budget
    plan: dict[str, list[tuple[str, str]]] = {}
    for ad in adapters:
        picks: list[tuple[str, str]] = []
        if ad.wants("archival"):
            picks += [(q, "archival") for q in archival]
        if ad.wants("atmospheric"):
            picks += [(q, "atmospheric") for q in atmospheric]
        plan[ad.name] = picks[:budget]
    return plan


def run(cfg: Config, script_path: str, job_id: str | None = None,
        kinds: list[str] | None = None, log=print) -> str:
    kinds = kinds or ["image"]
    store = Store(cfg.db_path)
    try:
        script = Path(script_path).read_text(encoding="utf-8")
        job_id = job_id or new_job_id(script_path)
        existing = store.get_job(job_id)
        store.create_job(job_id, script_path)

        adapters = build_adapters(cfg)
        log(f"[job {job_id}] adapters: {', '.join(a.name for a in adapters)}")

        # --- [1] segmentation (skip if resuming) ---------------------------
        beats = [b for b, _ in store.get_beats(job_id)]
        if not beats:
            log("[1] segmenting script...")
            beats = segment(cfg, script)
            for b in beats:
                store.save_beat(job_id, b, script[b.char_start:b.char_end])
            log(f"    -> {len(beats)} beats")
        else:
            log(f"[1] resuming with {len(beats)} existing beats")

        depictable = [b for b in beats if b.concreteness >= 0.25]
        if len(depictable) < len(beats):
            log(f"    ({len(beats) - len(depictable)} low-concreteness beats -> text card)")

        # --- [2] query generation + queue ----------------------------------
        if not store.pending_queries(job_id) and not _has_queries(store, job_id):
            log("[2] generating queries...")
            plans = generate(cfg, depictable)
            for b in depictable:
                bq = plans[b.beat_id]
                routed = _route_queries(cfg, adapters, bq.archival, bq.atmospheric)
                for source, picks in routed.items():
                    for query, family in picks:
                        store.queue_query(job_id, b.beat_id, source, query, family)

        # --- [3] execute pending queries (resumable) -----------------------
        pending = store.pending_queries(job_id)
        log(f"[3] {len(pending)} queries to run")
        if pending:
            asyncio.run(_run_queries(cfg, store, job_id, pending, adapters, kinds, log))

        # --- [4] fetch thumbnails ------------------------------------------
        log("[4] fetching thumbnails (cheap, no full assets)...")
        all_assets: list[Asset] = []
        for b in beats:
            all_assets += store.get_discovered_assets(job_id, b.beat_id)
        # dedupe by key before fetching
        uniq = {a.key: a for a in all_assets}
        tally = asyncio.run(fetch_thumbs(cfg, store, list(uniq.values()), log=log))
        log(f"    thumbs: {tally['ok']} new, {tally['cached']} cached, "
            f"{tally['fail']} failed, {tally['skip']} no-url")

        # --- [5] filter + rank ---------------------------------------------
        reranker = Reranker(cfg)
        if cfg.use_clip:
            log(f"[5] CLIP rerank: {'on' if reranker.ready else 'requested but unavailable'}")
        for b in beats:
            assets = store.get_discovered_assets(job_id, b.beat_id)
            kept, quarantined = rank_beat(cfg, store, b, assets, reranker)
            store.clear_candidates(job_id, b.beat_id)
            for c in kept:
                store.save_candidate(job_id, b.beat_id, c.asset.source, c.asset.source_id,
                                     c.score, c.rank, False, None)
            for c in quarantined:
                store.save_candidate(job_id, b.beat_id, c.asset.source, c.asset.source_id,
                                     0.0, 0, True, c.reject_reason)
            log(f"    {b.beat_id}: {len(kept)} candidates, {len(quarantined)} quarantined")

        store.set_job_status(job_id, "ready")
        log(f"[done] job {job_id} ready for review")
        return job_id
    finally:
        store.close()


def _has_queries(store: Store, job_id: str) -> bool:
    r = store.conn.execute(
        "SELECT 1 FROM queries WHERE job_id=? LIMIT 1", (job_id,)
    ).fetchone()
    return r is not None


async def _run_queries(cfg: Config, store: Store, job_id: str, pending: list,
                       adapters: list[Adapter], kinds: list[str], log) -> None:
    by_name = {a.name: a for a in adapters}
    sem = asyncio.Semaphore(12)  # global cap; adapters also self-throttle
    done = 0
    total = len(pending)

    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}) as client:
        async def one(row):
            nonlocal done
            adapter = by_name.get(row["source"])
            if adapter is None:
                store.mark_query(job_id, row["beat_id"], row["source"], row["query"], "done")
                return
            # Pixabay 24h cache: skip if fresh
            if adapter.name == "pixabay" and store.query_cache_fresh(
                    job_id, row["beat_id"], row["source"], row["query"], _PIXABAY_CACHE_S):
                return
            want = [k for k in kinds if k in adapter.supports]
            assets: list[Asset] = []
            try:
                async with sem:
                    for kind in want:
                        assets += await adapter.search(client, row["query"], kind,
                                                        cfg.limit_per_query)
                for a in assets:
                    store.upsert_asset(a)
                    store.add_discovered(job_id, row["beat_id"], a.source, a.source_id)
                store.conn.commit()
                store.mark_query(job_id, row["beat_id"], row["source"], row["query"], "done")
            except Exception as e:  # a failing adapter must not kill the run
                store.mark_query(job_id, row["beat_id"], row["source"], row["query"], "error")
                log(f"    ! {adapter.name} '{row['query']}': {type(e).__name__}")
            finally:
                done += 1
                if done % 20 == 0 or done == total:
                    log(f"    {done}/{total} queries")

        await asyncio.gather(*(one(r) for r in pending), return_exceptions=True)
