"""Command-line entry point.

  broll run script.md            # stages 1-5: script -> ranked candidates
  broll review                   # stage 6: local review UI
  broll export                   # stage 7: full-res download + treatment + credits
  broll jobs                     # list jobs
  broll sources                  # show adapters and which are enabled
"""

from __future__ import annotations

import sys

import typer

# Windows consoles default to cp1252; keep unicode in logs from crashing.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from .config import load_config
from .sources import build_adapters

app = typer.Typer(add_completion=False, help="B-Roll Pre-Pass")


@app.command()
def run(
    script: str = typer.Argument(..., help="Path to the script (.md/.txt)."),
    job: str = typer.Option(None, help="Job id (defaults to script name + timestamp). "
                                       "Re-use to resume."),
    video: bool = typer.Option(False, help="Also search for video (deliberately off by default)."),
    clip: bool = typer.Option(False, help="Enable CLIP rerank (needs the 'clip' extra installed)."),
):
    """Run the unattended batch: segment, query, fetch thumbnails, filter, rank."""
    from . import pipeline

    from .llm import describe_backend

    cfg = load_config()
    if clip:
        cfg.use_clip = True
    kinds = ["image", "video"] if video else ["image"]
    typer.echo(f"LLM backend: {describe_backend(cfg)}")
    job_id = pipeline.run(cfg, script, job_id=job, kinds=kinds)
    typer.echo(f"\nReview it:  broll review --job {job_id}")


@app.command()
def review(
    job: str = typer.Option(None, help="Job id to review (defaults to most recent)."),
    port: int = typer.Option(8000, help="Port for the local review server."),
):
    """Launch the local review UI (stage 6)."""
    import uvicorn

    from .ui.app import create_app

    cfg = load_config()
    application = create_app(cfg, job)
    typer.echo(f"Review UI at http://127.0.0.1:{port}  (Ctrl-C to stop)")
    uvicorn.run(application, host="127.0.0.1", port=port, log_level="warning")


@app.command()
def export(
    job: str = typer.Option(None, help="Job id to export (defaults to most recent)."),
    treat: bool = typer.Option(True, help="Apply the style pre-treatment chain."),
):
    """Download full-res selections, apply style treatment, write attribution.md (stage 7)."""
    from .export import export_job

    cfg = load_config()
    out = export_job(cfg, job, treat=treat)
    typer.echo(f"Exported to {out}")


@app.command()
def jobs():
    """List jobs in the store."""
    from .db import Store

    cfg = load_config()
    store = Store(cfg.db_path)
    rows = store.list_jobs()
    if not rows:
        typer.echo("No jobs yet. Run:  broll run script.md")
    for r in rows:
        typer.echo(f"{r['job_id']:32}  {r['status']:8}  {r['script_path']}")
    store.close()


@app.command()
def sources():
    """Show adapters and which are enabled with the current keys."""
    from .sources import all_adapter_names

    cfg = load_config()
    enabled = {a.name for a in build_adapters(cfg)}
    for name in all_adapter_names():
        mark = "on " if name in enabled else "off"
        typer.echo(f"  [{mark}] {name}")
    typer.echo("\nOff sources need an API key in .env (see .env.example).")


@app.command()
def llm():
    """Show which LLM backend the app will use for segmentation / query gen."""
    from .llm import describe_backend, resolve_backend

    cfg = load_config()
    typer.echo(f"  backend : {resolve_backend(cfg)}")
    typer.echo(f"  meaning : {describe_backend(cfg)}")
    typer.echo("\n  auto (default) uses your Claude subscription via the Claude Code")
    typer.echo("  CLI and never the metered API. Force with BROLL_LLM_BACKEND=")
    typer.echo("  subscription | offline | api.")


if __name__ == "__main__":
    app()
