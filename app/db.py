# -*- coding: utf-8 -*-
"""SQLite 持久化：炉次账（账套/炉/出货批/转料）、签单与核定结果。"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

DB_PATH = os.environ.get(
    "AUDIT_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "audit.db")
)
DB_PATH = os.path.abspath(DB_PATH)

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ledgers (
    id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS furnaces (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    primary_kg INTEGER NOT NULL CHECK (primary_kg >= 0),
    recycled_kg INTEGER NOT NULL CHECK (recycled_kg >= 0),
    UNIQUE (ledger_id, code)
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id) ON DELETE CASCADE,
    furnace_code TEXT NOT NULL,
    code TEXT NOT NULL,
    weight_kg INTEGER NOT NULL CHECK (weight_kg >= 0),
    UNIQUE (ledger_id, code)
);

CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id) ON DELETE CASCADE,
    src_furnace TEXT NOT NULL,
    dst_furnace TEXT NOT NULL,
    weight_kg INTEGER NOT NULL CHECK (weight_kg >= 0),
    CHECK (src_furnace <> dst_furnace)
);

-- 签单按出货批号签发；每个批号至多一张有效（active=1）签单。
-- 签单不绑账套：换账后批号仍在则继续生效，批号消失则新账无法核定。
CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY,
    batch_code TEXT NOT NULL,
    min_recycled_kg INTEGER NOT NULL CHECK (min_recycled_kg > 0),
    active INTEGER NOT NULL DEFAULT 1,
    signed_at TEXT NOT NULL,
    revoked_at TEXT
);
-- 每个批号至多一张有效签单（撤销后的历史记录不受限制）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_cert_active_batch
    ON certificates(batch_code) WHERE active = 1;

-- 每个账套最近一次核定结果（成功才落库，失败保留旧记录）。
CREATE TABLE IF NOT EXISTS solutions (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL UNIQUE REFERENCES ledgers(id) ON DELETE CASCADE,
    ok INTEGER NOT NULL,
    reason TEXT,
    solved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS solution_batches (
    id INTEGER PRIMARY KEY,
    solution_id INTEGER NOT NULL REFERENCES solutions(id) ON DELETE CASCADE,
    batch_code TEXT NOT NULL,
    recycled_kg INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS solution_transfers (
    id INTEGER PRIMARY KEY,
    solution_id INTEGER NOT NULL REFERENCES solutions(id) ON DELETE CASCADE,
    transfer_id INTEGER NOT NULL,
    recycled_kg INTEGER NOT NULL
);

-- 行级核定明细（按账套+批号/转料号唯一）：补签时只 UPDATE 重量有变的行，
-- 未变动行保持原值与 updated_at，便于审核员逐笔核对。
CREATE TABLE IF NOT EXISTS solution_batch_lines (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id) ON DELETE CASCADE,
    batch_code TEXT NOT NULL,
    recycled_kg INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (ledger_id, batch_code)
);

CREATE TABLE IF NOT EXISTS solution_transfer_lines (
    id INTEGER PRIMARY KEY,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id) ON DELETE CASCADE,
    transfer_id INTEGER NOT NULL,
    recycled_kg INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (ledger_id, transfer_id)
);

-- 补签变动台账：每次签发记录每一笔变动的核定明细行（或一条“跳过求解”事件）。
CREATE TABLE IF NOT EXISTS sign_changes (
    id INTEGER PRIMARY KEY,
    cert_id INTEGER REFERENCES certificates(id) ON DELETE SET NULL,
    ledger_id INTEGER NOT NULL REFERENCES ledgers(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,            -- batch / transfer
    target_ref TEXT NOT NULL,             -- 出货批号 或 转料记录号
    before_kg INTEGER NOT NULL,
    after_kg INTEGER NOT NULL,
    change_kg INTEGER NOT NULL,           -- after - before（带符号）
    skipped INTEGER NOT NULL DEFAULT 0,   -- 1=仅登记签单、跳过求解
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sign_changes_created ON sign_changes(created_at, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    success INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_ledger ON batches(ledger_id);
CREATE INDEX IF NOT EXISTS idx_transfers_ledger ON transfers(ledger_id);
CREATE INDEX IF NOT EXISTS idx_cert_active ON certificates(active);
"""


def utcnow_iso() -> str:
    # 含毫秒，避免同一秒内“只写变动行”的 updated_at 无法区分
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def log_event(conn: sqlite3.Connection, action: str, detail: str, success: bool) -> None:
    conn.execute(
        "INSERT INTO audit_log(ts, action, detail, success) VALUES (?, ?, ?, ?)",
        (utcnow_iso(), action, detail, 1 if success else 0),
    )


def current_ledger_id(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM ledgers WHERE is_current = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def load_ledger(
    conn: sqlite3.Connection, ledger_id: int
) -> Tuple[List[sqlite3.Row], List[sqlite3.Row], List[sqlite3.Row]]:
    furnaces = conn.execute(
        "SELECT * FROM furnaces WHERE ledger_id = ? ORDER BY code", (ledger_id,)
    ).fetchall()
    batches = conn.execute(
        "SELECT * FROM batches WHERE ledger_id = ? ORDER BY code", (ledger_id,)
    ).fetchall()
    transfers = conn.execute(
        "SELECT * FROM transfers WHERE ledger_id = ? ORDER BY id", (ledger_id,)
    ).fetchall()
    return furnaces, batches, transfers


def active_certificates(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificates WHERE active = 1 ORDER BY batch_code"
    ).fetchall()


def save_solution(
    conn: sqlite3.Connection,
    ledger_id: int,
    ok: bool,
    reason: Optional[str],
    batch_values: Iterable[Tuple[str, int]] = (),
    transfer_values: Iterable[Tuple[int, int]] = (),
) -> int:
    """覆盖式保存某账套的核定结果（调用方已在事务中）。"""
    old = conn.execute(
        "SELECT id FROM solutions WHERE ledger_id = ?", (ledger_id,)
    ).fetchone()
    if old:
        conn.execute("DELETE FROM solutions WHERE id = ?", (old["id"],))
    cur = conn.execute(
        "INSERT INTO solutions(ledger_id, ok, reason, solved_at) VALUES (?, ?, ?, ?)",
        (ledger_id, 1 if ok else 0, reason, utcnow_iso()),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO solution_batches(solution_id, batch_code, recycled_kg) "
        "VALUES (?, ?, ?)",
        [(sid, b, v) for b, v in batch_values],
    )
    conn.executemany(
        "INSERT INTO solution_transfers(solution_id, transfer_id, recycled_kg) "
        "VALUES (?, ?, ?)",
        [(sid, t, v) for t, v in transfer_values],
    )
    return sid


def get_solution(conn: sqlite3.Connection, ledger_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM solutions WHERE ledger_id = ?", (ledger_id,)
    ).fetchone()


def solution_details(
    conn: sqlite3.Connection, solution_id: int
) -> Tuple[List[sqlite3.Row], List[sqlite3.Row]]:
    sb = conn.execute(
        "SELECT * FROM solution_batches WHERE solution_id = ? ORDER BY batch_code",
        (solution_id,),
    ).fetchall()
    st = conn.execute(
        "SELECT * FROM solution_transfers WHERE solution_id = ? ORDER BY transfer_id",
        (solution_id,),
    ).fetchall()
    return sb, st


# ---------------------------------------------------------------------------
# 行级核定明细（补签最小变动的持久化基础；已存在账套自动迁移，无需重建）
# ---------------------------------------------------------------------------
def migrate_solution_lines(conn: sqlite3.Connection) -> None:
    """把旧版 solution_batches/solution_transfers 一次性迁入行级明细表。

    仅填充行级表中尚不存在的 (账套, 行) 键，幂等，可对已有账套直接执行。
    """
    now = utcnow_iso()
    conn.execute(
        "INSERT OR IGNORE INTO solution_batch_lines"
        "(ledger_id, batch_code, recycled_kg, updated_at) "
        "SELECT s.ledger_id, sb.batch_code, sb.recycled_kg, s.solved_at "
        "FROM solution_batches sb JOIN solutions s ON s.id = sb.solution_id",
    )
    conn.execute(
        "INSERT OR IGNORE INTO solution_transfer_lines"
        "(ledger_id, transfer_id, recycled_kg, updated_at) "
        "SELECT s.ledger_id, st.transfer_id, st.recycled_kg, s.solved_at "
        "FROM solution_transfers st JOIN solutions s ON s.id = st.solution_id",
    )
    # 账套内存在但旧明细表缺行的键（理论上不会出现，兜底补 0，updated_at 固定）
    conn.execute(
        "INSERT OR IGNORE INTO solution_batch_lines"
        "(ledger_id, batch_code, recycled_kg, updated_at) "
        "SELECT b.ledger_id, b.code, 0, ? FROM batches b", (now,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO solution_transfer_lines"
        "(ledger_id, transfer_id, recycled_kg, updated_at) "
        "SELECT t.ledger_id, t.id, 0, ? FROM transfers t", (now,),
    )


def baseline_lines(
    conn: sqlite3.Connection, ledger_id: int
) -> Tuple[Dict[str, int], Dict[int, int]]:
    """读取某账套签发前的核定基准（批 -> kg，转料行号 -> kg）。"""
    batches = conn.execute(
        "SELECT batch_code, recycled_kg FROM solution_batch_lines WHERE ledger_id = ?",
        (ledger_id,),
    ).fetchall()
    transfers = conn.execute(
        "SELECT transfer_id, recycled_kg FROM solution_transfer_lines "
        "WHERE ledger_id = ?",
        (ledger_id,),
    ).fetchall()
    return (
        {r["batch_code"]: r["recycled_kg"] for r in batches},
        {r["transfer_id"]: r["recycled_kg"] for r in transfers},
    )


def save_solution_lines_full(
    conn: sqlite3.Connection,
    ledger_id: int,
    batch_values: Iterable[Tuple[str, int]],
    transfer_values: Iterable[Tuple[int, int]],
) -> None:
    """整体重核路径：覆盖式 UPSERT 全部行级明细。"""
    now = utcnow_iso()
    conn.executemany(
        "INSERT INTO solution_batch_lines(ledger_id, batch_code, recycled_kg, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(ledger_id, batch_code) DO UPDATE SET "
        "recycled_kg = excluded.recycled_kg, updated_at = excluded.updated_at",
        [(ledger_id, b, v, now) for b, v in batch_values],
    )
    conn.executemany(
        "INSERT INTO solution_transfer_lines"
        "(ledger_id, transfer_id, recycled_kg, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(ledger_id, transfer_id) DO UPDATE SET "
        "recycled_kg = excluded.recycled_kg, updated_at = excluded.updated_at",
        [(ledger_id, t, v, now) for t, v in transfer_values],
    )


def apply_min_change_lines(
    conn: sqlite3.Connection,
    ledger_id: int,
    new_batches: Dict[str, int],
    new_transfers: Dict[int, int],
    baseline_batches: Dict[str, int],
    baseline_transfers: Dict[int, int],
) -> List[dict]:
    """补签路径：只写入重量有变的核定明细行，返回变动清单。

    未变动的行不 UPDATE，updated_at 保持旧值；变动清单按类型与行号排序，
    每项含 target_type/target_ref/before_kg/after_kg/change_kg。
    """
    now = utcnow_iso()
    changes: List[dict] = []
    for code in sorted(new_batches):
        old_v = baseline_batches[code]
        new_v = new_batches[code]
        if new_v != old_v:
            conn.execute(
                "UPDATE solution_batch_lines SET recycled_kg = ?, updated_at = ? "
                "WHERE ledger_id = ? AND batch_code = ?",
                (new_v, now, ledger_id, code),
            )
            changes.append({
                "target_type": "batch", "target_ref": code,
                "before_kg": old_v, "after_kg": new_v, "change_kg": new_v - old_v,
            })
    for tid in sorted(new_transfers):
        old_v = baseline_transfers[tid]
        new_v = new_transfers[tid]
        if new_v != old_v:
            conn.execute(
                "UPDATE solution_transfer_lines SET recycled_kg = ?, updated_at = ? "
                "WHERE ledger_id = ? AND transfer_id = ?",
                (new_v, now, ledger_id, tid),
            )
            changes.append({
                "target_type": "transfer", "target_ref": str(tid),
                "before_kg": old_v, "after_kg": new_v, "change_kg": new_v - old_v,
            })
    return changes


def record_sign_changes(
    conn: sqlite3.Connection,
    cert_id: Optional[int],
    ledger_id: int,
    changes: List[dict],
    skipped: bool,
) -> None:
    """登记补签变动台账。skipped 时写一条事件行（before=after=0）。"""
    now = utcnow_iso()
    if skipped:
        conn.execute(
            "INSERT INTO sign_changes"
            "(cert_id, ledger_id, target_type, target_ref, before_kg, after_kg, "
            " change_kg, skipped, created_at) VALUES (?, ?, 'event', '(跳过求解)', "
            "0, 0, 0, 1, ?)",
            (cert_id, ledger_id, now),
        )
        return
    conn.executemany(
        "INSERT INTO sign_changes"
        "(cert_id, ledger_id, target_type, target_ref, before_kg, after_kg, "
        " change_kg, skipped, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
        [
            (cert_id, ledger_id, c["target_type"], c["target_ref"],
             c["before_kg"], c["after_kg"], c["change_kg"], now)
            for c in changes
        ],
    )


def latest_sign_event_id(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT MAX(id) AS m FROM sign_changes").fetchone()
    return row["m"] if row and row["m"] is not None else None


def sign_changes_since(
    conn: sqlite3.Connection, event_id: int
) -> List[sqlite3.Row]:
    """取某次补签事件（同一批写入共用一个自增区间）的全部变动行。"""
    # 同一事件的行连续写入：以 created_at + 最大 id 反推该事件的最早 id。
    row = conn.execute(
        "SELECT created_at FROM sign_changes WHERE id = ?", (event_id,)
    ).fetchone()
    if not row:
        return []
    ts = row["created_at"]
    return conn.execute(
        "SELECT * FROM sign_changes WHERE created_at = ? ORDER BY id", (ts,)
    ).fetchall()


def recent_sign_changes(
    conn: sqlite3.Connection, limit: int = 30
) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sign_changes ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
