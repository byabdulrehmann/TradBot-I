"""Local persistence for read-only MT5 history and trader-entered reflections."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
from threading import RLock
from contextlib import closing

import sys


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


JOURNAL_PATH = _base_dir() / "memory" / "trading_journal.sqlite3"
_LOCK = RLock()
DAILY_CHECKIN_FIELDS = (
    "trading_mood",
    "market_bias",
    "intended_direction",
    "outside_mood",
    "confidence",
    "readiness",
    "stand_aside_conditions",
    "risk_commitment",
)


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = path or JOURNAL_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS mt5_trades (
            account_id TEXT NOT NULL,
            ticket TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            opened_at TEXT,
            closed_at TEXT NOT NULL,
            volume REAL NOT NULL,
            net REAL NOT NULL,
            PRIMARY KEY (account_id, ticket)
        );
        CREATE TABLE IF NOT EXISTS reflections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            ticket TEXT,
            category TEXT NOT NULL,
            symbol TEXT NOT NULL,
            setup TEXT NOT NULL,
            risk_pct REAL,
            emotional_state TEXT NOT NULL,
            plan_adherence TEXT NOT NULL,
            lesson TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mt5_sync_state (
            account_id TEXT PRIMARY KEY,
            last_sync_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS daily_checkins (
            checkin_date TEXT PRIMARY KEY,
            answers_json TEXT NOT NULL,
            complete INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS reflections_created_at
            ON reflections(account_id, created_at);
    """)
    return connection


def save_daily_checkin(
    checkin_date: str, answers: dict, *, db_path: Path | None = None
) -> dict:
    """Merge trader-reported answers for a local calendar-day check-in."""
    checkin_date = str(checkin_date or "").strip()
    try:
        datetime.strptime(checkin_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Check-in date must use YYYY-MM-DD format.") from exc
    if not isinstance(answers, dict):
        raise ValueError("Daily check-in answers must be an object.")

    submitted = {
        field: str(answers[field]).strip()[:1000]
        for field in DAILY_CHECKIN_FIELDS
        if field in answers and str(answers[field]).strip()
    }
    with _LOCK, closing(_connect(db_path)) as connection:
        with connection:
            row = connection.execute(
                "SELECT answers_json FROM daily_checkins WHERE checkin_date = ?",
                (checkin_date,),
            ).fetchone()
            merged = json.loads(row["answers_json"]) if row else {}
            merged.update(submitted)
            missing = [field for field in DAILY_CHECKIN_FIELDS if field not in merged]
            complete = not missing
            connection.execute("""
                INSERT INTO daily_checkins
                    (checkin_date, answers_json, complete, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(checkin_date) DO UPDATE SET
                    answers_json=excluded.answers_json,
                    complete=excluded.complete,
                    updated_at=excluded.updated_at
            """, (
                checkin_date,
                json.dumps(merged, ensure_ascii=False),
                int(complete),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ))
    return {
        "checkin_date": checkin_date,
        "answers": merged,
        "complete": complete,
        "missing": missing,
    }


def get_daily_checkin(
    checkin_date: str, *, db_path: Path | None = None
) -> dict | None:
    with _LOCK, closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT checkin_date, answers_json, complete, updated_at "
            "FROM daily_checkins WHERE checkin_date = ?",
            (str(checkin_date),),
        ).fetchone()
    if row is None:
        return None
    return {
        "checkin_date": row["checkin_date"],
        "answers": json.loads(row["answers_json"]),
        "complete": bool(row["complete"]),
        "updated_at": row["updated_at"],
    }


def get_recent_daily_checkins(
    limit: int = 7, *, db_path: Path | None = None
) -> list[dict]:
    limit = max(1, min(int(limit), 30))
    with _LOCK, closing(_connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT checkin_date, answers_json, complete, updated_at "
            "FROM daily_checkins WHERE complete = 1 "
            "ORDER BY checkin_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        {
            "checkin_date": row["checkin_date"],
            "answers": json.loads(row["answers_json"]),
            "complete": bool(row["complete"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def save_mt5_trades(
    account_id: str, trades: list[dict], *, db_path: Path | None = None
) -> int:
    account_id = str(account_id or "").strip()
    if not account_id:
        raise ValueError("A broker account identifier is required to save trade history.")
    values = []
    for trade in trades:
        ticket = str(trade.get("ticket", "")).strip()
        closed_at = str(trade.get("closed_at", "")).strip()
        if not ticket or not closed_at:
            continue
        values.append((
            account_id, ticket, str(trade.get("symbol", ""))[:32],
            str(trade.get("side", ""))[:8],
            str(trade.get("opened_at", "")) or None, closed_at,
            float(trade.get("volume", 0.0)), float(trade.get("net", 0.0)),
        ))
    if not values:
        return 0
    with _LOCK, closing(_connect(db_path)) as connection:
        with connection:
            connection.executemany("""
                INSERT INTO mt5_trades
                    (account_id, ticket, symbol, side, opened_at, closed_at, volume, net)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, ticket) DO UPDATE SET
                    symbol=excluded.symbol, side=excluded.side,
                    opened_at=excluded.opened_at, closed_at=excluded.closed_at,
                    volume=excluded.volume, net=excluded.net
            """, values)
    return len(values)


def save_reflection(
    account_id: str,
    category: str,
    symbol: str,
    setup: str = "",
    risk_pct: float | None = None,
    emotional_state: str = "",
    plan_adherence: str = "",
    lesson: str = "",
    ticket: str = "",
    *,
    db_path: Path | None = None,
) -> int:
    category = str(category or "").strip().lower()
    if category not in {"pre_trade", "trade_open", "post_trade"}:
        raise ValueError("Reflection category must be pre_trade, trade_open, or post_trade.")
    account_id = str(account_id or "").strip() or "unlinked"
    symbol = str(symbol or "").strip().upper()[:32]
    if not symbol:
        raise ValueError("A symbol is required for a trade reflection.")
    risk = None if risk_pct in (None, "") else float(risk_pct)
    with _LOCK, closing(_connect(db_path)) as connection:
        with connection:
            cursor = connection.execute("""
                INSERT INTO reflections
                    (account_id, ticket, category, symbol, setup, risk_pct,
                     emotional_state, plan_adherence, lesson, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                account_id, str(ticket or "").strip()[:64] or None, category, symbol,
                str(setup or "").strip()[:500], risk,
                str(emotional_state or "").strip()[:240],
                str(plan_adherence or "").strip()[:240],
                str(lesson or "").strip()[:500],
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ))
            return int(cursor.lastrowid)


def history_summary(
    account_id: str = "",
    days: int = 365,
    *,
    db_path: Path | None = None,
) -> dict:
    days = max(1, min(int(days), 3650))
    start = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    clauses = ["closed_at >= ?"]
    params: list[object] = [start]
    if account_id:
        clauses.append("account_id = ?")
        params.append(str(account_id))
    with _LOCK, closing(_connect(db_path)) as connection:
        trades = connection.execute(
            "SELECT * FROM mt5_trades WHERE " + " AND ".join(clauses)
            + " ORDER BY closed_at ASC",
            params,
        ).fetchall()
        reflection_query = "SELECT * FROM reflections"
        reflection_params: list[object] = []
        if account_id:
            reflection_query += " WHERE account_id = ?"
            reflection_params.append(str(account_id))
        reflections = connection.execute(
            reflection_query + " ORDER BY created_at DESC LIMIT 20",
            reflection_params,
        ).fetchall()

    nets = [float(row["net"]) for row in trades]
    positive = sum(value for value in nets if value > 0)
    negative = abs(sum(value for value in nets if value < 0))
    loss_streak = 0
    for value in reversed(nets):
        if value >= 0:
            break
        loss_streak += 1
    rapid_reentries = 0
    ordered = [dict(row) for row in trades]
    for previous, current in zip(ordered, ordered[1:]):
        if previous["net"] < 0 and previous["closed_at"] and current["opened_at"]:
            try:
                gap = (
                    datetime.fromisoformat(current["opened_at"])
                    - datetime.fromisoformat(previous["closed_at"])
                ).total_seconds()
            except ValueError:
                continue
            if 0 <= gap <= 180:
                rapid_reentries += 1

    return {
        "period_days": days,
        "trade_count": len(trades),
        "wins": sum(value > 0 for value in nets),
        "losses": sum(value < 0 for value in nets),
        "net": round(sum(nets), 2),
        "gross_profit": round(positive, 2),
        "gross_loss": round(negative, 2),
        "profit_factor": round(positive / negative, 3) if negative else None,
        "consecutive_losses": loss_streak,
        "rapid_reentries_after_loss": rapid_reentries,
        "reflections": [dict(row) for row in reflections],
    }


def daily_summary(account_id: str, *, db_path: Path | None = None) -> dict:
    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    clauses = ["closed_at >= ?"]
    params: list[object] = [start]
    if account_id:
        clauses.append("account_id = ?")
        params.append(str(account_id))
    with _LOCK, closing(_connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT net FROM mt5_trades WHERE " + " AND ".join(clauses),
            params,
        ).fetchall()
    values = [float(row["net"]) for row in rows]
    return {
        "closed_trades": len(values),
        "realized_loss": round(abs(sum(value for value in values if value < 0)), 2),
        "net": round(sum(values), 2),
    }


def history_sync_start(
    account_id: str, *, db_path: Path | None = None
) -> datetime:
    fallback = datetime(2000, 1, 1, tzinfo=timezone.utc)
    with _LOCK, closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT last_sync_at FROM mt5_sync_state WHERE account_id = ?",
            (str(account_id),),
        ).fetchone()
    if row is None:
        return fallback
    last_sync = datetime.fromisoformat(str(row["last_sync_at"]))
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)
    return max(fallback, last_sync - timedelta(days=2))


def mark_history_synced(
    account_id: str, *, db_path: Path | None = None
) -> None:
    with _LOCK, closing(_connect(db_path)) as connection:
        with connection:
            connection.execute("""
                INSERT INTO mt5_sync_state(account_id, last_sync_at)
                VALUES (?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    last_sync_at=excluded.last_sync_at
            """, (
                str(account_id), datetime.now(timezone.utc).isoformat(timespec="seconds")
            ))
