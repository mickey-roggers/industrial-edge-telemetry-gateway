"""Machine registry repository (reference solution).

Tracks the set of "known machines" the gateway has discovered from the
simulator, plus which register map each one uses. The poller consults this to
decide which devices to poll and which decode map to apply (§3.9).
"""
from __future__ import annotations

import sqlite3
from typing import Optional

import config


class MachineRepository:
    def __init__(self, db_path: str = config.DB_PATH):
        self._db_path = db_path
        self.conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS machines (
                   id           TEXT PRIMARY KEY,
                   register_map TEXT NOT NULL
               )"""
        )
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def add_machine(self, machine_id: str, register_map: str):
        self.conn.execute(
            """INSERT INTO machines (id, register_map) VALUES (?,?)
               ON CONFLICT(id) DO UPDATE SET register_map=excluded.register_map""",
            (machine_id, register_map),
        )
        self.conn.commit()

    def get_machine(self, machine_id: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute("SELECT * FROM machines WHERE id=?", (machine_id,))
        return cur.fetchone()

    def all_machines(self):
        cur = self.conn.execute("SELECT * FROM machines ORDER BY id")
        return cur.fetchall()

    def known_ids(self):
        return [r["id"] for r in self.all_machines()]

    def clear(self):
        self.conn.execute("DELETE FROM machines;")
        self.conn.commit()
