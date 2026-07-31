"""[6] Review UI. Local FastAPI serving one static page.

One row per beat: script text on the left, ranked contact sheet on the right.
Keyboard-driven, license badges on every tile, video hover-preview, quarantine
toggle. Writes selections.json — the only output of this stage.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..db import Store

_TEMPLATES = Path(__file__).parent / "templates"


def _license_class(license_id: str, attribution_required: bool) -> str:
    if license_id in ("cc0", "pd"):
        return "ok"          # green: free to use, no credit
    if license_id in ("pexels", "pixabay"):
        return "stock"       # blue: stock license, no credit
    if attribution_required or license_id.startswith("cc-"):
        return "credit"      # amber: attribution required
    return "unknown"         # red


def _pick_job(store: Store, job: str | None) -> str | None:
    if job:
        return job
    rows = store.list_jobs()
    ready = [r for r in rows if r["status"] == "ready"]
    if ready:
        return ready[0]["job_id"]
    return rows[0]["job_id"] if rows else None


def _tile(store: Store, cfg: Config, job_id: str, row) -> dict | None:
    asset = store.get_asset(row["source"], row["source_id"])
    if asset is None:
        return None
    arow = store.get_asset_row(row["source"], row["source_id"])
    thumb = None
    if arow and arow["thumb_path"] and Path(arow["thumb_path"]).exists():
        thumb = "/thumbs/" + Path(arow["thumb_path"]).name
    w, h = asset.width, asset.height
    if w and h:
        dims_label = f"{w}×{h}"          # e.g. 3913×2749
        dims_warn = w < cfg.min_width          # below the resolution gate
    else:
        dims_label = "size?"
        dims_warn = True
    return {
        "source": asset.source,
        "source_id": asset.source_id,
        "kind": asset.kind,
        "title": asset.title or "",
        "thumb": thumb,
        "width": w,
        "height": h,
        "dims_label": dims_label,
        "dims_warn": dims_warn,
        "portrait": bool(w and h and w < h),  # taller than wide (unknown => not portrait)
        "video": asset.full_url if asset.kind == "video" else None,
        "page_url": asset.page_url,
        "license_id": asset.license_id,
        "license_class": _license_class(asset.license_id, asset.attribution_required),
        "creator": asset.creator or "",
        "reject_reason": row["reject_reason"] or "",
        "selected": store.is_selected(job_id, row["beat_id"], asset.source, asset.source_id),
    }


def create_app(cfg: Config, job: str | None) -> FastAPI:
    app = FastAPI(title="B-Roll Pre-Pass Review")
    templates = Jinja2Templates(directory=str(_TEMPLATES))
    cfg.thumbs_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/thumbs", StaticFiles(directory=str(cfg.thumbs_dir)), name="thumbs")

    def store() -> Store:
        return Store(cfg.db_path)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        st = store()
        try:
            job_id = _pick_job(st, job)
            if not job_id:
                return HTMLResponse("<h1>No jobs. Run: <code>broll run script.md</code></h1>")
            beats = st.get_beats(job_id)
            view = []
            for beat, script_text in beats:
                cands = st.get_candidates(job_id, beat.beat_id, include_quarantined=True)
                tiles, quarantine = [], []
                for row in cands:
                    t = _tile(st, cfg, job_id, row)
                    if t is None:
                        continue
                    (quarantine if row["quarantined"] else tiles).append(t)
                view.append({
                    "beat": beat, "script_text": script_text,
                    "tiles": tiles, "quarantine": quarantine,
                })
            return templates.TemplateResponse(
                request, "review.html",
                {"job_id": job_id, "beats": view,
                 "landscape_default": cfg.landscape_only},
            )
        finally:
            st.close()

    @app.post("/select")
    async def select(request: Request):
        body = await request.json()
        st = store()
        try:
            selected = st.toggle_selection(
                body["job_id"], body["beat_id"], body["source"], body["source_id"])
            return JSONResponse({"selected": selected})
        finally:
            st.close()

    @app.get("/selections.json")
    def selections(job_id: str):
        st = store()
        try:
            out = _dump_selections(st, cfg, job_id)
            return JSONResponse(out)
        finally:
            st.close()

    return app


def _dump_selections(store: Store, cfg: Config, job_id: str) -> dict:
    rows = store.get_selections(job_id)
    items = []
    for r in rows:
        asset = store.get_asset(r["source"], r["source_id"])
        if not asset:
            continue
        items.append({
            "beat_id": r["beat_id"], "source": asset.source, "source_id": asset.source_id,
            "kind": asset.kind, "title": asset.title, "page_url": asset.page_url,
            "full_url": asset.full_url, "license_id": asset.license_id,
            "attribution_required": asset.attribution_required,
            "creator": asset.creator, "creator_url": asset.creator_url,
        })
    payload = {"job_id": job_id, "selections": items}
    out_path = cfg.data_dir / f"selections_{job_id}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def write_selections_file(cfg: Config, job_id: str) -> Path:
    """Used by the export stage to materialize selections.json from the DB."""
    st = Store(cfg.db_path)
    try:
        _dump_selections(st, cfg, job_id)
    finally:
        st.close()
    return cfg.data_dir / f"selections_{job_id}.json"
