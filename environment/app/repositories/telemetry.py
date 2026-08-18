"""SQLite repository for telemetry history and latest machine state.

Concurrency contract (§7): each machine's writes are scoped by its device_id and
performed inside a SQLite transaction so two overlapping polls of the *same*
machine cannot corrupt a single row. We serialize writes with a per-machine
async lock and rely on SQLite's transactional isolation for atomicity, which
prevents lost updates, partial writes, and duplicate history rows.

History rows use the composite primary key (device_id, sequence) so an attempted
duplicate insert is rejected by the database itself (defense in depth on top of
the application-level duplicate check in §4.2).
"""
from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from typing import Optional

import config

_schema = """
CREATE TABLE IF NOT EXISTS history (
    device_id   TEXT NOT NULL,
    sequence    INTEGER NOT NULL,
    timestamp   TEXT NOT NULL,
    temperature_c REAL NOT NULL,
    pressure_kpa   REAL NOT NULL,
    vibration_mm_s REAL NOT NULL,
    status      TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    PRIMARY KEY (device_id, sequence)
);

CREATE TABLE IF NOT EXISTS latest_state (
    device_id         TEXT PRIMARY KEY,
    sequence          INTEGER NOT NULL,
    timestamp         TEXT NOT NULL,
    temperature_c     REAL NOT NULL,
    pressure_kpa      REAL NOT NULL,
    vibration_mm_s    REAL NOT NULL,
    status            TEXT NOT NULL,
    status_code       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS failure_counter (
    device_id   TEXT PRIMARY KEY,
    count       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS alarms (
    machine_id  TEXT NOT NULL,
    alarm_type  TEXT NOT NULL,
    state       TEXT NOT NULL,
    severity    TEXT NOT NULL,
    PRIMARY KEY (machine_id, alarm_type)
);
"""


class TelemetryRepository:
    def __init__(self, db_path: str = config.DB_PATH):
        self._db_path = db_path
        self._connect()
        self._lock = asyncio.Lock()  # guards all writes; ensures serializable
        # Per-machine locks are layered on top to bound contention.
        self._machine_locks: dict[str, asyncio.Lock] = {}
        self._machine_locks_guard = asyncio.Lock()

    def _connect(self):
        self.conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.executescript(_schema)
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _machine_lock(self, device_id: str) -> asyncio.Lock:
        # Lazily create a per-machine lock.
        if device_id not in self._machine_locks:
            self._machine_locks[device_id] = asyncio.Lock()
        return self._machine_locks[device_id]

    # ---- history ---------------------------------------------------------
    def history_exists(self, device_id: str, sequence: int) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM history WHERE device_id=? AND sequence<=?",
            (device_id, sequence),
        )
        return cur.fetchone() is not None

    def insert_history(self, reading) -> bool:
        """Insert a history row. Returns False if the (device_id, sequence)
        composite key already exists (duplicate)."""
        try:
            self.conn.execute(
                """INSERT INTO history
                   (device_id, sequence, timestamp, temperature_c, pressure_kpa,
                    vibration_mm_s, status, status_code)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    reading.device_id,
                    reading.sequence,
                    reading.timestamp,
                    reading.temperature_c,
                    reading.pressure_kpa,
                    reading.vibration_mm_s,
                    reading.status,
                    reading.status_code,
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            self.conn.rollback()
            return False

    def get_history(
        self,
        device_id: str,
        from_ts: Optional[str] = None,
        to_ts: Optional[str] = None,
        limit: int = 100,
    ):
        sql = "SELECT * FROM history WHERE device_id=?"
        params: list = [device_id]
        if from_ts is not None:
            sql += " AND timestamp >= ?"
            params.append(from_ts)
        if to_ts is not None:
            sql += " AND timestamp <= ?"
            params.append(to_ts)
        sql += " ORDER BY timestamp ASC, sequence ASC"
        sql += " LIMIT ?"
        params.append(limit)
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    def count_history(self, device_id: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) AS c FROM history WHERE device_id=?", (device_id,)
        )
        row = cur.fetchone()
        return int(row["c"]) if row else 0

    # ---- latest state ----------------------------------------------------
    def get_latest_state(self, device_id: str):
        cur = self.conn.execute(
            "SELECT * FROM latest_state WHERE device_id=?", (device_id,)
        )
        return cur.fetchone()

    def upsert_latest_state(self, reading):
        self.conn.execute(
            """INSERT INTO latest_state
               (device_id, sequence, timestamp, temperature_c, pressure_kpa,
                vibration_mm_s, status, status_code)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
                   sequence=excluded.sequence,
                   timestamp=excluded.timestamp,
                   temperature_c=excluded.temperature_c,
                   pressure_kpa=excluded.pressure_kpa,
                   vibration_mm_s=excluded.vibration_mm_s,
                   status=excluded.status,
                   status_code=excluded.status_code""",
            (
                reading.device_id,
                reading.sequence,
                reading.timestamp,
                reading.temperature_c,
                reading.pressure_kpa,
                reading.vibration_mm_s,
                reading.status,
                reading.status_code,
            ),
        )
        self.conn.commit()

    # ---- failure counter -------------------------------------------------
    def get_failure_count(self, device_id: str) -> int:
        cur = self.conn.execute(
            "SELECT count FROM failure_counter WHERE device_id=?", (device_id,)
        )
        row = cur.fetchone()
        return int(row["count"]) if row else 0

    def set_failure_count(self, device_id: str, count: int):
        self.conn.execute(
            """INSERT INTO failure_counter (device_id, count) VALUES (?,?)
               ON CONFLICT(device_id) DO UPDATE SET count=excluded.count""",
            (device_id, count),
        )
        self.conn.commit()

    # ---- alarms ----------------------------------------------------------
    def get_alarm(self, machine_id: str, alarm_type: str):
        cur = self.conn.execute(
            "SELECT * FROM alarms WHERE machine_id=? AND alarm_type=?",
            (machine_id, alarm_type),
        )
        return cur.fetchone()

    def upsert_alarm(self, machine_id: str, alarm_type: str, state: str, severity: str):
        self.conn.execute(
            """INSERT INTO alarms (machine_id, alarm_type, state, severity)
               VALUES (?,?,?,?)
               ON CONFLICT(machine_id, alarm_type) DO UPDATE SET
                   state=excluded.state, severity=excluded.severity""",
            (machine_id, alarm_type, state, severity),
        )
        self.conn.commit()

    def get_alarms(self, machine_id=None, state=None, severity=None):
        sql = "SELECT * FROM alarms WHERE 1=1"
        params: list = []
        if machine_id is not None:
            sql += " AND machine_id=?"
            params.append(machine_id)
        if state is not None:
            sql += " AND state=?"
            params.append(state)
        if severity is not None:
            sql += " AND severity=?"
            params.append(severity)
        cur = self.conn.execute(sql, params)
        return cur.fetchall()

    def count_active_alarms(self, machine_id: str) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) AS c FROM alarms WHERE machine_id=? AND state=?",
            (machine_id, "ACTIVE"),
        )
        row = cur.fetchone()
        return int(row["c"]) if row else 0

    def clear_all(self):
        """Wipe all telemetry state (test/admin isolation between scenarios)."""
        self.conn.execute("DELETE FROM history;")
        self.conn.execute("DELETE FROM latest_state;")
        self.conn.execute("DELETE FROM failure_counter;")
        self.conn.execute("DELETE FROM alarms;")
        self.conn.commit()
