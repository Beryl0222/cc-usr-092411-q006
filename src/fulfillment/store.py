"""SQLite 事件存储与幂等/收发件箱。

设计要点：

- ``event_store`` 只追加事件，不原地改写；按 (aggregate_id, version) 乐观锁追加。
- ``serial_slots`` 由 SERIAL_HELD/SERIAL_* 事件维护唯一占用：序号同一时刻只能被
  一条未释放预占持有，并发预占依赖该表的事务级唯一约束分胜负。
- ``receipt_index`` 记录外部回执（支付回执、登记回执、退件回执）的首次结果；
  同一回执完全重放返回原结果；关键内容变化按重复回执冲突拒绝。
- ``registration_outbox`` 持久化待上链/待补登记的登记请求，重启后继续处理。
- ``return_inbox`` 持久化物流退回通知，重启后继续处理。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from .events import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_store (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT,
    event_type     TEXT NOT NULL,
    aggregate_type TEXT NOT NULL,
    aggregate_id   TEXT NOT NULL,
    version        INTEGER NOT NULL,
    occurred_at    TEXT NOT NULL,
    causation_id   TEXT,
    summary        TEXT NOT NULL,
    payload        TEXT NOT NULL,
    UNIQUE(aggregate_id, version)
);

CREATE TABLE IF NOT EXISTS serial_slots (
    edition_id TEXT NOT NULL,
    serial     INTEGER NOT NULL,
    state      TEXT NOT NULL,
    hold_id    TEXT NOT NULL,
    order_id   TEXT,
    updated_seq INTEGER NOT NULL,
    PRIMARY KEY (edition_id, serial)
);

CREATE TABLE IF NOT EXISTS receipt_index (
    receipt_kind TEXT NOT NULL,           -- payment | registration | return
    receipt_ref  TEXT NOT NULL,
    outcome      TEXT NOT NULL,           -- 首次处理的结果签名
    request_key  TEXT NOT NULL,           -- 首次请求的关键内容签名
    created_at   TEXT NOT NULL,
    PRIMARY KEY (receipt_kind, receipt_ref)
);

CREATE TABLE IF NOT EXISTS idempotency_index (
    command_key TEXT PRIMARY KEY,         -- 应用层命令幂等键（预占/订单等）
    result_ref  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registration_outbox (
    registration_ref TEXT PRIMARY KEY,
    order_id         TEXT NOT NULL,
    state            TEXT NOT NULL,       -- PENDING | ACK_ACCEPTED | ACK_FAILED
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS return_inbox (
    return_id    TEXT PRIMARY KEY,
    shipment_no  TEXT NOT NULL,
    state        TEXT NOT NULL,           -- RECEIVED | PROCESSED
    reason       TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
"""


class ReceiptConflict(Exception):
    """同一回执携带了与首次不一致的关键内容。"""


class SerialSlotTaken(Exception):
    """序号已被另一条预占占用（并发竞争失败）。"""


class EventStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # 单连接 + RLock：一个写事务整体持锁，读写不会与其他线程交错
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # -- 事件 -------------------------------------------------------------- #
    def append_events(self, conn: sqlite3.Connection, events: Iterable[Event]) -> None:
        import dataclasses
        import uuid as _uuid
        for event in events:
            if event.event_id is None:
                event = dataclasses.replace(event, event_id=f"evt-{_uuid.uuid4().hex[:16]}")
            row = event.to_row()
            conn.execute(
                """INSERT INTO event_store
                   (event_id, event_type, aggregate_type, aggregate_id, version,
                    occurred_at, causation_id, summary, payload)
                   VALUES (:event_id, :event_type, :aggregate_type, :aggregate_id, :version,
                           :occurred_at, :causation_id, :summary, :payload)""",
                row,
            )
            self._project_slot(conn, event, conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    @staticmethod
    def _project_slot(conn: sqlite3.Connection, event: Event, seq: int) -> None:
        p = event.payload
        t = event.event_type
        if t == "SERIAL_HELD":
            # 唯一占位：占用中的序号在此冲突；已释放的序号允许重新预占
            cur = conn.execute(
                """INSERT INTO serial_slots (edition_id, serial, state, hold_id, order_id, updated_seq)
                   VALUES (?, ?, 'HELD', ?, NULL, ?)
                   ON CONFLICT(edition_id, serial) DO UPDATE SET
                       state='HELD', hold_id=excluded.hold_id, order_id=NULL,
                       updated_seq=excluded.updated_seq
                   WHERE serial_slots.state='RELEASED'""",
                (p["edition_id"], p["serial"], event.aggregate_id, seq),
            )
            if cur.rowcount == 0:
                raise SerialSlotTaken(f"序号 {p['serial']} 已被占用")
        elif t == "SERIAL_HOLD_EXPIRED":
            conn.execute(
                "UPDATE serial_slots SET state='RELEASED', updated_seq=? WHERE edition_id=? AND serial=?",
                (seq, p["edition_id"], p["serial"]),
            )
        elif t == "SERIAL_RESERVATION_LINKED":
            conn.execute(
                "UPDATE serial_slots SET state='LINKED', order_id=?, updated_seq=? WHERE edition_id=? AND serial=?",
                (p["order_id"], seq, p["edition_id"], p["serial"]),
            )
        elif t == "SERIAL_RESERVATION_RELEASED":
            conn.execute(
                "UPDATE serial_slots SET state='RELEASED', order_id=NULL, updated_seq=? WHERE edition_id=? AND serial=?",
                (seq, p["edition_id"], p["serial"]),
            )
        elif t == "SERIAL_OWNERSHIP_REGISTERED":
            conn.execute(
                "UPDATE serial_slots SET state='REGISTERED', order_id=COALESCE(?, order_id), updated_seq=? "
                "WHERE edition_id=? AND serial=?",
                (p.get("order_id"), seq, p["edition_id"], p["serial"]),
            )

    def load_events(self, aggregate_id: str) -> list[Event]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM event_store WHERE aggregate_id=? ORDER BY version", (aggregate_id,)
            ).fetchall()
        return [Event.from_row(dict(r)) for r in rows]

    def load_events_by_type(self, aggregate_type: str, event_types: tuple[str, ...]) -> list[Event]:
        marks = ",".join("?" for _ in event_types)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM event_store WHERE aggregate_type=? AND event_type IN ({marks}) ORDER BY seq",
                (aggregate_type, *event_types),
            ).fetchall()
        return [Event.from_row(dict(r)) for r in rows]

    def all_events(self) -> list[Event]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM event_store ORDER BY seq").fetchall()
        return [Event.from_row(dict(r)) for r in rows]

    def rebuild_slots(self) -> None:
        """重启后从事件流重建序号占用表（事件为唯一事实来源）。"""
        with self.transaction() as conn:
            conn.execute("DELETE FROM serial_slots")
            rows = conn.execute("SELECT * FROM event_store ORDER BY seq").fetchall()
            for row in rows:
                self._project_slot(conn, Event.from_row(dict(row)), row["seq"])

    # -- 序号槽 ------------------------------------------------------------- #
    def slot_state(self, conn: sqlite3.Connection, edition_id: str, serial: int) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT * FROM serial_slots WHERE edition_id=? AND serial=?", (edition_id, serial)
        ).fetchone()
        return dict(row) if row else None

    def read_slot(self, edition_id: str, serial: int) -> dict[str, Any] | None:
        with self._lock:
            return self.slot_state(self._conn, edition_id, serial)

    def first_free_serial(self, conn: sqlite3.Connection, edition_id: str, sellable: list[int]) -> int | None:
        """挑出可售且当前槽位为空或已释放的最小序号。"""
        for serial in sellable:
            slot = self.slot_state(conn, edition_id, serial)
            if slot is None or slot["state"] == "RELEASED":
                return serial
        return None

    # -- 回执幂等 ----------------------------------------------------------- #
    @staticmethod
    def _canonical(value: Any) -> str:
        import json
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def check_receipt(self, conn: sqlite3.Connection, kind: str, ref: str, request_key: dict[str, Any]) -> dict[str, Any] | None:
        """重放检测：未见过返回 None；完全重放返回原结果；内容变化抛 ReceiptConflict。"""
        key = self._canonical(request_key)
        existing = conn.execute(
            "SELECT outcome, request_key FROM receipt_index WHERE receipt_kind=? AND receipt_ref=?",
            (kind, ref),
        ).fetchone()
        if existing is None:
            return None
        if existing["request_key"] != key:
            raise ReceiptConflict(f"回执 {kind}:{ref} 的请求内容与首次不一致")
        import json
        return json.loads(existing["outcome"])

    def remember_receipt(
        self,
        conn: sqlite3.Connection,
        kind: str,
        ref: str,
        request_key: dict[str, Any],
        outcome: dict[str, Any],
        now_iso: str,
    ) -> None:
        conn.execute(
            """INSERT INTO receipt_index (receipt_kind, receipt_ref, outcome, request_key, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (kind, ref, self._canonical(outcome), self._canonical(request_key), now_iso),
        )

    def receipt_outcome(self, kind: str, ref: str) -> dict[str, Any] | None:
        import json
        with self._lock:
            row = self._conn.execute(
                "SELECT outcome FROM receipt_index WHERE receipt_kind=? AND receipt_ref=?", (kind, ref)
            ).fetchone()
        return json.loads(row["outcome"]) if row else None

    # -- 命令幂等 ----------------------------------------------------------- #
    def remember_command(self, conn: sqlite3.Connection, key: str, result_ref: str, now_iso: str) -> bool:
        try:
            conn.execute(
                "INSERT INTO idempotency_index (command_key, result_ref, created_at) VALUES (?, ?, ?)",
                (key, result_ref, now_iso),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def command_result(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_ref FROM idempotency_index WHERE command_key=?", (key,)
            ).fetchone()
        return row["result_ref"] if row else None

    # -- 登记 outbox / 退件 inbox ------------------------------------------- #
    def enqueue_registration(self, conn, registration_ref: str, order_id: str, now_iso: str) -> None:
        conn.execute(
            """INSERT INTO registration_outbox (registration_ref, order_id, state, created_at, updated_at)
               VALUES (?, ?, 'PENDING', ?, ?)
               ON CONFLICT(registration_ref) DO NOTHING""",
            (registration_ref, order_id, now_iso, now_iso),
        )

    def pending_registrations(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM registration_outbox WHERE state='PENDING' ORDER BY rowid"
            ).fetchall()
        return [dict(r) for r in rows]

    def touch_registration_attempt(self, conn, registration_ref: str, error: str | None, now_iso: str) -> None:
        conn.execute(
            "UPDATE registration_outbox SET attempts=attempts+1, last_error=?, updated_at=? WHERE registration_ref=?",
            (error, now_iso, registration_ref),
        )

    def settle_registration(self, conn, registration_ref: str, state: str, now_iso: str) -> None:
        conn.execute(
            "UPDATE registration_outbox SET state=?, last_error=NULL, updated_at=? WHERE registration_ref=?",
            (state, now_iso, registration_ref),
        )

    def receive_return(self, conn, return_id: str, shipment_no: str, reason: str, now_iso: str) -> bool:
        """新退件返回 True；已存在的同一退件回执返回 False（重放）。"""
        cur = conn.execute(
            """INSERT INTO return_inbox (return_id, shipment_no, state, reason, received_at)
               VALUES (?, ?, 'RECEIVED', ?, ?)
               ON CONFLICT(return_id) DO NOTHING""",
            (return_id, shipment_no, reason, now_iso),
        )
        return cur.rowcount == 1

    def pending_returns(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM return_inbox WHERE state='RECEIVED' ORDER BY rowid"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_return_processed(self, conn, return_id: str) -> None:
        conn.execute("UPDATE return_inbox SET state='PROCESSED' WHERE return_id=?", (return_id,))

    def return_processed(self, return_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM return_inbox WHERE return_id=?", (return_id,)
            ).fetchone()
        return row is not None and row["state"] == "PROCESSED"
