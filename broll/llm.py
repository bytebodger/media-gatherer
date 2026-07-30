"""LLM layer for structured-output calls (segmentation + query generation).

Three backends, chosen by `Config.llm_backend`:

  "subscription" — shell out to the Claude Code CLI (`claude -p`). Uses YOUR
                   logged-in Claude account, NOT the metered API. The API key is
                   explicitly stripped from the subprocess so it can never be
                   billed to pay-per-token API usage.
  "api"          — the metered Anthropic API (needs ANTHROPIC_API_KEY). Opt-in only.
  "offline"      — never call a model; callers use their rule-based fallback.
  "auto"         — subscription if the `claude` CLI is present, else offline.
                   NEVER silently falls back to the metered API.

Any failure raises LLMUnavailable, so callers fall back to the offline heuristics
in segment.py / queries.py rather than crashing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .config import Config

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    pass


def _cli_path() -> str | None:
    return shutil.which("claude")


def resolve_backend(cfg: Config) -> str:
    """Turn 'auto' into a concrete backend. Auto never chooses the metered API."""
    backend = (cfg.llm_backend or "auto").lower()
    if backend == "auto":
        return "subscription" if _cli_path() else "offline"
    return backend


def available(cfg: Config) -> bool:
    """True if a model-calling backend is usable. Used by callers to decide
    whether to attempt an LLM pass before falling back to offline heuristics."""
    backend = resolve_backend(cfg)
    if backend == "subscription":
        return _cli_path() is not None
    if backend == "api":
        return bool(cfg.anthropic_api_key)
    return False


def describe_backend(cfg: Config) -> str:
    backend = resolve_backend(cfg)
    return {
        "subscription": "Claude subscription via Claude Code CLI (not billed to the API)",
        "api": "metered Anthropic API (ANTHROPIC_API_KEY)",
        "offline": "offline rule-based (no model calls)",
    }.get(backend, backend)


def parse_into(cfg: Config, system: str, user: str, schema: type[T]) -> T:
    """One structured-output call, returning a validated instance of `schema`."""
    backend = resolve_backend(cfg)
    if backend == "subscription":
        return _via_cli(cfg, system, user, schema)
    if backend == "api":
        return _via_api(cfg, system, user, schema)
    raise LLMUnavailable(f"backend '{backend}' does not call a model")


# --- subscription backend: Claude Code CLI -----------------------------------

def _via_cli(cfg: Config, system: str, user: str, schema: type[T]) -> T:
    cli = _cli_path()
    if not cli:
        raise LLMUnavailable("claude CLI not found on PATH")

    prompt = (
        f"{system}\n\n"
        "Respond with ONLY a single JSON object — no prose, no markdown fences — "
        f"that validates against this JSON schema:\n{json.dumps(schema.model_json_schema())}\n\n"
        f"{user}"
    )

    # Strip API-key env vars so the CLI uses the logged-in SUBSCRIPTION, never
    # the metered API. This is the whole point of this backend.
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}

    cmd = [cli, "-p", "--output-format", "json"]
    if cfg.llm_model:
        cmd += ["--model", cfg.llm_model]
    try:
        proc = subprocess.run(
            cmd, input=prompt, env=env, capture_output=True, text=True,
            timeout=cfg.llm_timeout_s, encoding="utf-8",
        )
    except subprocess.TimeoutExpired as e:
        raise LLMUnavailable(f"claude CLI timed out after {cfg.llm_timeout_s}s") from e
    if proc.returncode != 0:
        raise LLMUnavailable(f"claude CLI exited {proc.returncode}: {proc.stderr[:200]}")

    # Envelope: {"type":"result","result":"...text...", ...}
    try:
        envelope = json.loads(proc.stdout)
        text = envelope.get("result", "") if isinstance(envelope, dict) else proc.stdout
    except json.JSONDecodeError:
        text = proc.stdout
    return _extract(text, schema)


def _extract(text: str, schema: type[T]) -> T:
    """Pull a JSON object out of a model response and validate it."""
    text = text.strip()
    # strip ```json ... ``` fences if present
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    candidates = [text]
    # also try the outermost {...} span
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            return schema.model_validate_json(cand)
        except (ValidationError, ValueError):
            continue
    raise LLMUnavailable(f"could not parse {schema.__name__} from model output")


# --- metered API backend (opt-in only) ---------------------------------------

def _via_api(cfg: Config, system: str, user: str, schema: type[T]) -> T:
    if not cfg.anthropic_api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY not set")
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    resp = client.messages.parse(
        model=cfg.llm_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=system,
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    )
    if resp.parsed_output is None:
        raise LLMUnavailable(f"model did not return valid {schema.__name__}")
    return resp.parsed_output
