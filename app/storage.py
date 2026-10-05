"""SQLite 持久层。

设计要点：
- events 按 event_id 全局唯一，重放可识别；事件行只插入不更新（不可变流水）。
- determinations 只追加（UNIQUE(order_id, version)），已归档判定永不改写；
  更正事件只会产生更高 version 的新判定。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import date
from typing import Any

from .timeutil import iso_z, now_utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
  code        TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holiday_calendars (
  code         TEXT NOT NULL REFERENCES calendars(code),
  holiday_date TEXT NOT NULL,               -- YYYY-MM-DD，订单时区当地日期
  name         TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (code, holiday_date)
);
CREATE TABLE IF NOT EXISTS policies (
  version                TEXT PRIMARY KEY,
  effective_from         TEXT NOT NULL,     -- YYYY-MM-DD，按订单创建当日（当地日期）生效
  claim_window_type      TEXT NOT NULL,     -- calendar_days | business_days
  claim_window_value     INTEGER NOT NULL,
  damage_photo_required  INTEGER NOT NULL,
  description            TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS orders (
  order_id         TEXT PRIMARY KEY,
  created_at       TEXT NOT NULL,           -- ISO-8601 UTC
  timezone         TEXT NOT NULL,           -- IANA 时区名
  promise_type     TEXT NOT NULL,           -- hours | business_days
  promise_value    INTEGER NOT NULL,
  holiday_calendar TEXT NOT NULL,
  policy_version   TEXT NOT NULL,           -- 创建时解析并固化
  promised_by      TEXT NOT NULL,           -- 承诺送达截止，ISO-8601 UTC
  recorded_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  event_id              TEXT PRIMARY KEY,
  order_id              TEXT NOT NULL REFERENCES orders(order_id),
  seq                   INTEGER NOT NULL,   -- 订单内到达序号
  type                  TEXT NOT NULL,
  occurred_at           TEXT NOT NULL,      -- 事件发生时间（更正事件为更正登记时间）
  payload_json          TEXT NOT NULL,
  corrects_event_id     TEXT,               -- 仅 CORRECTION：被更正的事件
  corrected_occurred_at TEXT,               -- 仅 CORRECTION：更正后的发生时间
  corrected_type        TEXT,               -- 仅 CORRECTION：更正后的类型（可空）
  payload_hash          TEXT NOT NULL,      -- 幂等重放的内容指纹
  received_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id, seq);
CREATE TABLE IF NOT EXISTS determinations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id    TEXT NOT NULL REFERENCES orders(order_id),
  version     INTEGER NOT NULL,             -- = 该订单已应用事件数
  recorded_at TEXT NOT NULL,
  result_json TEXT NOT NULL,
  UNIQUE(order_id, version)
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")

    def init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)

    # ---------- 政策版本 ----------
    def upsert_policy(self, p: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO policies VALUES (:version, :effective_from,"
                " :claim_window_type, :claim_window_value, :damage_photo_required, :description)",
                p,
            )

    def list_policies(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM policies ORDER BY effective_from"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_policy(self, version: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM policies WHERE version = ?", (version,)
            ).fetchone()
        return dict(row) if row else None

    def resolve_policy(self, local_date: date) -> dict[str, Any] | None:
        """订单创建日（当地日期）当天已生效的最新政策版本。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM policies WHERE effective_from <= ?"
                " ORDER BY effective_from DESC LIMIT 1",
                (local_date.isoformat(),),
            ).fetchone()
        return dict(row) if row else None

    # ---------- 节假日表 ----------
    def ensure_calendar(self, code: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO calendars VALUES (?, ?)", (code, iso_z(now_utc()))
            )

    def add_holiday(self, code: str, day: str, name: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO holiday_calendars VALUES (?, ?, ?)", (code, day, name)
            )

    def replace_calendar(self, code: str, holidays: list[dict[str, str]]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO calendars VALUES (?, ?)", (code, iso_z(now_utc()))
            )
            self._conn.execute("DELETE FROM holiday_calendars WHERE code = ?", (code,))
            self._conn.executemany(
                "INSERT INTO holiday_calendars VALUES (?, ?, ?)",
                [(code, h["date"], h.get("name", "")) for h in holidays],
            )

    def list_calendars(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT code FROM calendars ORDER BY code").fetchall()
        return [r["code"] for r in rows]

    def calendar_exists(self, code: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM calendars WHERE code = ?", (code,)
            ).fetchone()
        return row is not None

    def get_holidays(self, code: str) -> set[date]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT holiday_date FROM holiday_calendars WHERE code = ?", (code,)
            ).fetchall()
        return {date.fromisoformat(r["holiday_date"]) for r in rows}

    def list_holiday_entries(self, code: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT holiday_date, name FROM holiday_calendars WHERE code = ?"
                " ORDER BY holiday_date",
                (code,),
            ).fetchall()
        return [{"date": r["holiday_date"], "name": r["name"]} for r in rows]

    # ---------- 订单 ----------
    def insert_order(self, o: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO orders VALUES (:order_id, :created_at,"
                               " :timezone, :promise_type, :promise_value, :holiday_calendar,"
                               " :policy_version, :promised_by, :recorded_at)", o)

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return dict(row) if row else None

    # ---------- 事件 ----------
    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_events(self, order_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE order_id = ? ORDER BY seq", (order_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def count_events(self, order_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE order_id = ?", (order_id,)
            ).fetchone()
        return int(row["c"])

    def has_correction_for(self, target_event_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE corrects_event_id = ?", (target_event_id,)
            ).fetchone()
        return row is not None

    def insert_event(self, e: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events VALUES (:event_id, :order_id, :seq, :type, :occurred_at,"
                " :payload_json, :corrects_event_id, :corrected_occurred_at, :corrected_type,"
                " :payload_hash, :received_at)",
                e,
            )

    # ---------- 判定快照（只追加，不改写） ----------
    def insert_determination(self, order_id: str, version: int, result: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO determinations (order_id, version, recorded_at, result_json)"
                " VALUES (?, ?, ?, ?)",
                (order_id, version, iso_z(now_utc()),
                 json.dumps(result, ensure_ascii=False, sort_keys=True)),
            )

    def list_determinations(self, order_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM determinations WHERE order_id = ? ORDER BY version", (order_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 统计 ----------
    def stats(self) -> dict[str, int]:
        with self._lock:
            out = {}
            for table in ("policies", "calendars", "holiday_calendars",
                          "orders", "events", "determinations"):
                out[table] = self._conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table}"  # noqa: S608 - 表名为常量
                ).fetchone()["c"]
        return out


def payload_fingerprint(*parts: Any) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
