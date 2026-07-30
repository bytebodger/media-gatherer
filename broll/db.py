"""SQLite job store. Buys resumability, ToS caching, and cross-video corpus reuse.

`assets` and `embeddings` are GLOBAL (not per-job). That's the corpus.
Everything else is scoped to a job.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .models import Asset, Beat

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    script_path TEXT,
    created_at  REAL,
    status      TEXT
);

CREATE TABLE IF NOT EXISTS beats (
    job_id        TEXT,
    beat_id       TEXT,
    char_start    INTEGER,
    char_end      INTEGER,
    summary       TEXT,
    visual_intent TEXT,
    era           TEXT,
    entities      TEXT,   -- json list
    concreteness  REAL,
    script_text   TEXT,
    PRIMARY KEY (job_id, beat_id)
);

CREATE TABLE IF NOT EXISTS queries (
    job_id     TEXT,
    beat_id    TEXT,
    source     TEXT,
    query      TEXT,
    family     TEXT,
    status     TEXT,     -- pending | done | error
    fetched_at REAL,
    PRIMARY KEY (job_id, beat_id, source, query)
);

-- Global corpus. NOT per-job. This compounds across videos.
CREATE TABLE IF NOT EXISTS assets (
    source     TEXT,
    source_id  TEXT,
    kind       TEXT,
    payload    TEXT,     -- full Asset json
    phash      TEXT,     -- perceptual hash of thumbnail
    thumb_path TEXT,
    PRIMARY KEY (source, source_id)
);

CREATE TABLE IF NOT EXISTS candidates (
    job_id     TEXT,
    beat_id    TEXT,
    source     TEXT,
    source_id  TEXT,
    score      REAL,
    rank       INTEGER,
    quarantined INTEGER DEFAULT 0,
    reject_reason TEXT,
    PRIMARY KEY (job_id, beat_id, source, source_id)
);

CREATE TABLE IF NOT EXISTS selections (
    job_id      TEXT,
    beat_id     TEXT,
    source      TEXT,
    source_id   TEXT,
    selected_at REAL,
    PRIMARY KEY (job_id, beat_id, source, source_id)
);

CREATE TABLE IF NOT EXISTS embeddings (
    source    TEXT,
    source_id TEXT,
    vec       BLOB,
    PRIMARY KEY (source, source_id)
);

-- Which assets a beat's queries surfaced (pre-rank). Survives restarts so a
-- resumed run can rank without re-hitting the sources.
CREATE TABLE IF NOT EXISTS discovered (
    job_id    TEXT,
    beat_id   TEXT,
    source    TEXT,
    source_id TEXT,
    PRIMARY KEY (job_id, beat_id, source, source_id)
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- jobs ---------------------------------------------------------------
    def create_job(self, job_id: str, script_path: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO jobs(job_id, script_path, created_at, status) VALUES (?,?,?,?)",
            (job_id, script_path, time.time(), "running"),
        )
        self.conn.commit()

    def set_job_status(self, job_id: str, status: str) -> None:
        self.conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, job_id))
        self.conn.commit()

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

    def list_jobs(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()

    # ---- beats --------------------------------------------------------------
    def save_beat(self, job_id: str, beat: Beat, script_text: str) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO beats
               (job_id, beat_id, char_start, char_end, summary, visual_intent, era,
                entities, concreteness, script_text)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id, beat.beat_id, beat.char_start, beat.char_end, beat.summary,
                beat.visual_intent, beat.era, json.dumps(beat.entities),
                beat.concreteness, script_text,
            ),
        )
        self.conn.commit()

    def get_beats(self, job_id: str) -> list[tuple[Beat, str]]:
        rows = self.conn.execute(
            "SELECT * FROM beats WHERE job_id=? ORDER BY char_start", (job_id,)
        ).fetchall()
        out = []
        for r in rows:
            beat = Beat(
                beat_id=r["beat_id"], char_start=r["char_start"], char_end=r["char_end"],
                summary=r["summary"], visual_intent=r["visual_intent"], era=r["era"],
                entities=json.loads(r["entities"] or "[]"), concreteness=r["concreteness"],
            )
            out.append((beat, r["script_text"] or ""))
        return out

    # ---- queries (checkpoint granularity) -----------------------------------
    def queue_query(self, job_id: str, beat_id: str, source: str, query: str, family: str) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO queries
               (job_id, beat_id, source, query, family, status, fetched_at)
               VALUES (?,?,?,?,?, 'pending', NULL)""",
            (job_id, beat_id, source, query, family),
        )
        self.conn.commit()

    def pending_queries(self, job_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM queries WHERE job_id=? AND status='pending'", (job_id,)
        ).fetchall()

    def mark_query(self, job_id: str, beat_id: str, source: str, query: str, status: str) -> None:
        self.conn.execute(
            "UPDATE queries SET status=?, fetched_at=? WHERE job_id=? AND beat_id=? AND source=? AND query=?",
            (status, time.time(), job_id, beat_id, source, query),
        )
        self.conn.commit()

    # ---- assets (global corpus + 24h cache) ---------------------------------
    def upsert_asset(self, asset: Asset, phash: str | None = None,
                     thumb_path: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO assets(source, source_id, kind, payload, phash, thumb_path)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(source, source_id) DO UPDATE SET
                 payload=excluded.payload,
                 phash=COALESCE(excluded.phash, assets.phash),
                 thumb_path=COALESCE(excluded.thumb_path, assets.thumb_path)""",
            (asset.source, asset.source_id, asset.kind, asset.model_dump_json(),
             phash, thumb_path),
        )
        self.conn.commit()

    def get_asset(self, source: str, source_id: str) -> Asset | None:
        r = self.conn.execute(
            "SELECT payload FROM assets WHERE source=? AND source_id=?", (source, source_id)
        ).fetchone()
        return Asset.model_validate_json(r["payload"]) if r else None

    def get_asset_row(self, source: str, source_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM assets WHERE source=? AND source_id=?", (source, source_id)
        ).fetchone()

    def query_cache_fresh(self, job_id: str, beat_id: str, source: str, query: str,
                          max_age_s: float) -> bool:
        r = self.conn.execute(
            "SELECT status, fetched_at FROM queries WHERE job_id=? AND beat_id=? AND source=? AND query=?",
            (job_id, beat_id, source, query),
        ).fetchone()
        if not r or r["status"] != "done" or not r["fetched_at"]:
            return False
        return (time.time() - r["fetched_at"]) < max_age_s

    # ---- discovered (beat -> asset link, pre-rank) --------------------------
    def add_discovered(self, job_id: str, beat_id: str, source: str, source_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO discovered(job_id, beat_id, source, source_id) VALUES (?,?,?,?)",
            (job_id, beat_id, source, source_id),
        )
        # committed by the caller in batches

    def get_discovered_assets(self, job_id: str, beat_id: str) -> list[Asset]:
        rows = self.conn.execute(
            """SELECT a.payload FROM discovered d
               JOIN assets a ON a.source=d.source AND a.source_id=d.source_id
               WHERE d.job_id=? AND d.beat_id=?""",
            (job_id, beat_id),
        ).fetchall()
        return [Asset.model_validate_json(r["payload"]) for r in rows]

    # ---- candidates ---------------------------------------------------------
    def clear_candidates(self, job_id: str, beat_id: str) -> None:
        self.conn.execute(
            "DELETE FROM candidates WHERE job_id=? AND beat_id=?", (job_id, beat_id)
        )
        self.conn.commit()

    def save_candidate(self, job_id: str, beat_id: str, source: str, source_id: str,
                       score: float, rank: int, quarantined: bool,
                       reject_reason: str | None) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO candidates
               (job_id, beat_id, source, source_id, score, rank, quarantined, reject_reason)
               VALUES (?,?,?,?,?,?,?,?)""",
            (job_id, beat_id, source, source_id, score, rank,
             1 if quarantined else 0, reject_reason),
        )
        self.conn.commit()

    def get_candidates(self, job_id: str, beat_id: str,
                       include_quarantined: bool = False) -> list[sqlite3.Row]:
        q = "SELECT * FROM candidates WHERE job_id=? AND beat_id=?"
        if not include_quarantined:
            q += " AND quarantined=0"
        q += " ORDER BY quarantined, rank"
        return self.conn.execute(q, (job_id, beat_id)).fetchall()

    # ---- selections ---------------------------------------------------------
    def toggle_selection(self, job_id: str, beat_id: str, source: str, source_id: str) -> bool:
        existing = self.conn.execute(
            "SELECT 1 FROM selections WHERE job_id=? AND beat_id=? AND source=? AND source_id=?",
            (job_id, beat_id, source, source_id),
        ).fetchone()
        if existing:
            self.conn.execute(
                "DELETE FROM selections WHERE job_id=? AND beat_id=? AND source=? AND source_id=?",
                (job_id, beat_id, source, source_id),
            )
            self.conn.commit()
            return False
        self.conn.execute(
            "INSERT INTO selections(job_id, beat_id, source, source_id, selected_at) VALUES (?,?,?,?,?)",
            (job_id, beat_id, source, source_id, time.time()),
        )
        self.conn.commit()
        return True

    def get_selections(self, job_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM selections WHERE job_id=? ORDER BY beat_id", (job_id,)
        ).fetchall()

    def is_selected(self, job_id: str, beat_id: str, source: str, source_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM selections WHERE job_id=? AND beat_id=? AND source=? AND source_id=?",
            (job_id, beat_id, source, source_id),
        ).fetchone() is not None

    # ---- embeddings ---------------------------------------------------------
    def save_embedding(self, source: str, source_id: str, vec: bytes) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO embeddings(source, source_id, vec) VALUES (?,?,?)",
            (source, source_id, vec),
        )
        self.conn.commit()

    def get_embedding(self, source: str, source_id: str) -> bytes | None:
        r = self.conn.execute(
            "SELECT vec FROM embeddings WHERE source=? AND source_id=?", (source, source_id)
        ).fetchone()
        return r["vec"] if r else None
