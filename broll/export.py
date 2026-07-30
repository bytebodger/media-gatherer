"""[7] Export. Second batch pass. Reads the DB selections.

- Downloads full-res, names files so they sort into timeline order.
- Style pre-treatment: a fixed Pillow/ffmpeg chain (duotone, grain, vignette).
  Keeps the untreated original alongside the treated version.
- Attribution manifest: attribution.md with every asset requiring credit.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import httpx

from .config import Config
from .db import Store
from .models import Asset
from .sources.base import USER_AGENT
from .ui.app import write_selections_file

try:
    from PIL import Image, ImageDraw, ImageFilter
    _HAVE_PIL = True
except Exception:
    _HAVE_PIL = False


def _pick_job(store: Store, job: str | None) -> str | None:
    if job:
        return job
    rows = store.list_jobs()
    return rows[0]["job_id"] if rows else None


def _safe(text: str, n: int = 40) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", (text or "")).strip("_")
    return text[:n] or "untitled"


def _beat_num(beat_id: str) -> str:
    m = re.search(r"(\d+)", beat_id)
    return m.group(1).zfill(3) if m else "000"


def _ext(url: str | None, kind: str) -> str:
    if url:
        suffix = Path(url.split("?")[0]).suffix.lower()
        if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff",
                      ".mp4", ".mov", ".webm", ".m4v"):
            return suffix
    return ".mp4" if kind == "video" else ".jpg"


def _resolve_full_url(client: httpx.Client, asset: Asset) -> str | None:
    url = asset.full_url or asset.thumb_url
    if not url:
        return None
    # Internet Archive full_url is a download DIRECTORY (HTML), not a file.
    # Resolve to the largest actual image/video file via the item metadata.
    if asset.source == "internetarchive":
        try:
            meta = client.get(f"https://archive.org/metadata/{asset.source_id}",
                              timeout=30).json()
            files = meta.get("files", []) if isinstance(meta, dict) else []
            want = (".mp4", ".mov", ".webm") if asset.kind == "video" \
                else (".jpg", ".jpeg", ".png", ".tif", ".tiff")
            picks = [f for f in files
                     if str(f.get("name", "")).lower().endswith(want)]
            picks.sort(key=lambda f: int(f.get("size", 0) or 0), reverse=True)
            if picks:
                return f"https://archive.org/download/{asset.source_id}/{picks[0]['name']}"
        except Exception:
            pass
        return asset.thumb_url  # services/img is a real (smaller) image
    # NASA gives a collection manifest (.json) of asset renditions.
    if asset.source == "nasa" and url.endswith(".json"):
        try:
            items = client.get(url, timeout=30).json()
            if isinstance(items, list):
                orig = [u for u in items if isinstance(u, str) and "orig" in u.lower()]
                cand = orig or [u for u in items if isinstance(u, str)
                                and u.lower().endswith((".jpg", ".png", ".mp4"))]
                if cand:
                    return cand[0]
        except Exception:
            return asset.thumb_url or None
    return url


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _treat_image(cfg: Config, src: Path, dst: Path) -> bool:
    if not _HAVE_PIL:
        return False
    try:
        img = Image.open(src).convert("L")  # grayscale
        shadow = _hex_to_rgb(cfg.duotone_shadow)
        highlight = _hex_to_rgb(cfg.duotone_highlight)
        # duotone: map luminance between shadow and highlight
        lut_r, lut_g, lut_b = [], [], []
        for i in range(256):
            t = i / 255.0
            lut_r.append(int(shadow[0] + (highlight[0] - shadow[0]) * t))
            lut_g.append(int(shadow[1] + (highlight[1] - shadow[1]) * t))
            lut_b.append(int(shadow[2] + (highlight[2] - shadow[2]) * t))
        duo = Image.merge("RGB", (img.point(lut_r), img.point(lut_g), img.point(lut_b)))
        if cfg.grain:
            import random
            noise = Image.effect_noise(duo.size, cfg.grain).convert("L")
            duo = Image.blend(duo, Image.merge("RGB", (noise, noise, noise)), 0.06)
        if cfg.vignette:
            duo = _vignette(duo, cfg.vignette)
        duo.save(dst, quality=92)
        return True
    except Exception:
        return False


def _vignette(img, strength: float):
    from PIL import Image, ImageDraw
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse([-w * 0.2, -h * 0.2, w * 1.2, h * 1.2], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(w, h) // 8))
    black = Image.new("RGB", (w, h), (0, 0, 0))
    faded = Image.composite(img, black, mask)
    return Image.blend(img, faded, strength)


def _treat_video(cfg: Config, src: Path, dst: Path) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vf", cfg.ffmpeg_vf,
             "-c:a", "copy", str(dst)],
            check=True, capture_output=True,
        )
        return True
    except Exception:
        return False


def export_job(cfg: Config, job: str | None, treat: bool = True) -> Path:
    store = Store(cfg.db_path)
    try:
        job_id = _pick_job(store, job)
        if not job_id:
            raise SystemExit("No job to export. Run: broll run script.md")
        write_selections_file(cfg, job_id)  # materialize selections.json for the record

        out_dir = cfg.exports_dir / job_id
        orig_dir = out_dir / "original"
        treat_dir = out_dir / "treated"
        orig_dir.mkdir(parents=True, exist_ok=True)
        if treat:
            treat_dir.mkdir(parents=True, exist_ok=True)

        rows = store.get_selections(job_id)
        if not rows:
            print("No selections. Open the review UI and pick some tiles first.")
            return out_dir

        credits: list[Asset] = []
        with httpx.Client(follow_redirects=True,
                          headers={"User-Agent": USER_AGENT}) as client:
            for r in rows:
                asset = store.get_asset(r["source"], r["source_id"])
                if not asset:
                    continue
                url = _resolve_full_url(client, asset)
                if not url:
                    print(f"  ! no full-res url for {asset.source}:{asset.source_id}")
                    continue
                name = (f"beat_{_beat_num(r['beat_id'])}_{asset.source}_"
                        f"{_safe(asset.title or asset.source_id)}")
                ext = _ext(url, asset.kind)
                orig = orig_dir / f"{name}{ext}"
                try:
                    resp = client.get(url, timeout=120)
                    resp.raise_for_status()
                    orig.write_bytes(resp.content)
                except Exception as e:
                    print(f"  ! download failed {asset.source}:{asset.source_id} ({type(e).__name__})")
                    continue

                if treat:
                    treated = treat_dir / f"{name}{ext}"
                    ok = (_treat_video(cfg, orig, treated) if asset.kind == "video"
                          else _treat_image(cfg, orig, treated))
                    if not ok:
                        shutil.copy2(orig, treated)  # keep a treated slot even if chain skipped
                if asset.attribution_required:
                    credits.append(asset)
                print(f"  [ok] {name}{ext}")

        _write_attribution(out_dir, job_id, credits)
        print(f"\nExported {len(rows)} selections. Untreated originals kept in {orig_dir}")
        return out_dir
    finally:
        store.close()


def _write_attribution(out_dir: Path, job_id: str, assets: list[Asset]) -> None:
    """attribution.md formatted for an end card. The one reason CC-BY is annoying —
    automate it once and CC-BY becomes as easy as CC0."""
    lines = [f"# Attribution — {job_id}", ""]
    if not assets:
        lines.append("_No selected assets require attribution (all CC0 / public domain / stock)._")
    else:
        lines.append("The following assets require a credit on your end card:\n")
        for a in assets:
            who = a.creator or "Unknown"
            src = f"[{a.source}]({a.page_url})" if a.page_url else a.source
            lic = f"[{a.license_id}]({a.license_url})" if a.license_url else a.license_id
            title = a.title or a.source_id
            lines.append(f"- **{title}** — {who}, via {src} ({lic})")
    (out_dir / "attribution.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
