import sqlite3
import json
from datetime import datetime
from pathlib import Path
from config import DB_PATH


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategies (
                name        TEXT PRIMARY KEY,
                blueprint   TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'parsed',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                source_file TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deployments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy    TEXT NOT NULL,
                mode        TEXT NOT NULL DEFAULT 'paper',
                started_at  TEXT NOT NULL,
                ended_at    TEXT,
                pnl         REAL,
                trades      INTEGER DEFAULT 0,
                log         TEXT
            )
        """)


def save_blueprint(name: str, blueprint: dict, source_file: str = "") -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO strategies (name, blueprint, status, created_at, updated_at, source_file)
            VALUES (?, ?, 'parsed', ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                blueprint  = excluded.blueprint,
                status     = 'parsed',
                updated_at = excluded.updated_at,
                source_file = excluded.source_file
        """, (name, json.dumps(blueprint), now, now, source_file))


def get_blueprint(name: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT blueprint FROM strategies WHERE name = ?", (name,)
        ).fetchone()
    return json.loads(row["blueprint"]) if row else None


def update_status(name: str, status: str) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as conn:
        conn.execute(
            "UPDATE strategies SET status = ?, updated_at = ? WHERE name = ?",
            (status, now, name),
        )


def list_strategies() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT name, status, created_at, updated_at, source_file FROM strategies ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def log_deployment(strategy: str, mode: str, pnl: float, trades: int, log: str) -> None:
    now = datetime.utcnow().isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO deployments (strategy, mode, started_at, ended_at, pnl, trades, log)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (strategy, mode, now, now, pnl, trades, log))
        conn.execute(
            "UPDATE strategies SET status = 'deployed', updated_at = ? WHERE name = ?",
            (now, strategy),
        )


init_db()
