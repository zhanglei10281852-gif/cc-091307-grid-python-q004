"""SQLite 持久化层：隐患、事件留痕、派发、复核与人员。

所有写操作由服务层在锁内调用；隐患更新采用
``UPDATE ... WHERE id=? AND version=?`` 的乐观并发控制。
"""
from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hazards (
    id TEXT PRIMARY KEY,
    building TEXT NOT NULL,
    unit TEXT NOT NULL,
    room TEXT NOT NULL,
    household_name TEXT NOT NULL,
    household_phone TEXT NOT NULL,
    authorization_json TEXT NOT NULL,
    device_json TEXT NOT NULL,
    items_json TEXT NOT NULL,
    status TEXT NOT NULL,
    branch TEXT NOT NULL DEFAULT 'none',
    branch_note TEXT NOT NULL DEFAULT '',
    assignee TEXT,
    deadline TEXT NOT NULL,
    review_verdict TEXT,
    discovered_by TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    hazard_id TEXT NOT NULL,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'online',
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS dispatches (
    id TEXT PRIMARY KEY,
    hazard_id TEXT NOT NULL,
    request_id TEXT UNIQUE,
    assignee TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id TEXT PRIMARY KEY,
    hazard_id TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    passed INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    buildings_json TEXT NOT NULL
);
"""


class SQLiteStore:
    """隐患闭环数据的 SQLite 存取。"""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------------- 隐患 ----------------
    def insert_hazard(self, row: dict) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        self._conn.execute(
            f"INSERT INTO hazards ({cols}) VALUES ({marks})", tuple(row.values())
        )
        self._conn.commit()

    def get_hazard(self, hazard_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM hazards WHERE id=?", (hazard_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_hazards(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM hazards ORDER BY created_at, id"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_hazard(self, hazard_id: str, expected_version: int, fields: dict) -> bool:
        """按版本号 CAS 更新；返回是否成功（False 即版本冲突）。"""
        cols = ", ".join(f"{k}=?" for k in fields)
        cur = self._conn.execute(
            f"UPDATE hazards SET {cols}, version=version+1 WHERE id=? AND version=?",
            (*fields.values(), hazard_id, expected_version),
        )
        self._conn.commit()
        return cur.rowcount == 1

    # ---------------- 事件留痕 ----------------
    def append_event(self, event: dict) -> None:
        cols = ", ".join(event)
        marks = ", ".join("?" for _ in event)
        self._conn.execute(
            f"INSERT INTO events ({cols}) VALUES ({marks})", tuple(event.values())
        )
        self._conn.commit()

    def get_event_by_id(self, event_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_events(self, hazard_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE hazard_id=? ORDER BY seq", (hazard_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 派发 ----------------
    def insert_dispatch(self, dispatch: dict) -> None:
        cols = ", ".join(dispatch)
        marks = ", ".join("?" for _ in dispatch)
        self._conn.execute(
            f"INSERT INTO dispatches ({cols}) VALUES ({marks})", tuple(dispatch.values())
        )
        self._conn.commit()

    def get_dispatch_by_request(self, request_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM dispatches WHERE request_id=?", (request_id,)
        ).fetchone()
        return dict(row) if row else None

    def latest_dispatch(self, hazard_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM dispatches WHERE hazard_id=? ORDER BY rowid DESC LIMIT 1",
            (hazard_id,),
        ).fetchone()
        return dict(row) if row else None

    # ---------------- 复核 ----------------
    def insert_review(self, review: dict) -> None:
        cols = ", ".join(review)
        marks = ", ".join("?" for _ in review)
        self._conn.execute(
            f"INSERT INTO reviews ({cols}) VALUES ({marks})", tuple(review.values())
        )
        self._conn.commit()

    def list_reviews(self, hazard_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM reviews WHERE hazard_id=? ORDER BY rowid", (hazard_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------- 人员 ----------------
    def upsert_worker(self, worker: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO workers (id, name, role, buildings_json) "
            "VALUES (:id, :name, :role, :buildings_json)",
            worker,
        )
        self._conn.commit()

    def get_worker(self, worker_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM workers WHERE id=?", (worker_id,)
        ).fetchone()
        return dict(row) if row else None
