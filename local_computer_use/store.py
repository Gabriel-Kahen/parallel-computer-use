"""The local run history. Calls are serialized by the orchestrator."""

import json
import sqlite3
from collections import defaultdict
from pathlib import Path


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(directory / "runs.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, snapshot TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL
                    REFERENCES runs(id) ON DELETE CASCADE,
                at TEXT NOT NULL, job_id TEXT, kind TEXT NOT NULL, text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq DESC);
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL
                    REFERENCES runs(id) ON DELETE CASCADE,
                at TEXT NOT NULL, sender TEXT NOT NULL, target TEXT NOT NULL, text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_run ON messages(run_id, id DESC);
        """)
        if "status" not in {column[1] for column in self.db.execute("PRAGMA table_info(runs)")}:
            with self.db:
                self.db.execute("ALTER TABLE runs ADD COLUMN status TEXT NOT NULL DEFAULT ''")
                records = self.db.execute("SELECT id, snapshot FROM runs").fetchall()
                self.db.executemany("UPDATE runs SET status=? WHERE id=?",
                                    [(json.loads(row["snapshot"])["status"], row["id"]) for row in records])

    def load(self):
        rows = self.db.execute("""
            WITH recent AS (
                SELECT snapshot, created_at FROM runs
                WHERE status IN ('completed', 'failed', 'cancelled', 'interrupted', 'partial')
                ORDER BY created_at DESC LIMIT 100
            )
            SELECT snapshot, created_at FROM recent
            UNION ALL SELECT snapshot, created_at FROM runs
            WHERE status NOT IN ('completed', 'failed', 'cancelled', 'interrupted', 'partial')
            ORDER BY created_at DESC
        """)
        return [json.loads(row["snapshot"]) for row in rows]

    def save(self, snapshot, event=None):
        with self.db:
            self.db.execute("""
                INSERT INTO runs(id, created_at, snapshot, status) VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot, status=excluded.status
            """, (snapshot["id"], snapshot["created_at"],
                  json.dumps(snapshot, separators=(",", ":")), snapshot["status"]))
            if event:
                self.db.execute("""
                    INSERT INTO events(run_id, at, job_id, kind, text) VALUES(?, ?, ?, ?, ?)
                """, (snapshot["id"], *event))

    def record_message(self, run_id, at, sender, target, text):
        with self.db:
            row = self.db.execute("""
                INSERT INTO messages(run_id, at, sender, target, text) VALUES(?, ?, ?, ?, ?)
            """, (run_id, at, sender, target, text))
        return row.lastrowid

    def activity(self, run_ids):
        events, messages = defaultdict(list), defaultdict(list)
        for run_id in run_ids:
            for row in self.db.execute("""
                SELECT seq, at, job_id, kind, text FROM events
                WHERE run_id=? ORDER BY seq DESC LIMIT 24
            """, (run_id,)):
                events[run_id].append(dict(row))
            for row in self.db.execute("""
                SELECT id, at, sender, target, text FROM messages
                WHERE run_id=? ORDER BY id DESC LIMIT 12
            """, (run_id,)):
                messages[run_id].append(dict(row))
        return events, messages

    def inbox(self, run_id, job_id, after):
        return [dict(row) for row in self.db.execute("""
            SELECT id, sender, text FROM messages WHERE run_id=? AND
            target IN (?, 'all') AND id>? ORDER BY id LIMIT 2
        """, (run_id, job_id, after))]

    def close(self):
        self.db.close()

    def older_runs(self, cutoff):
        rows = self.db.execute("""
            SELECT snapshot FROM runs
            WHERE created_at < ? AND status IN ('completed','failed','cancelled','interrupted','partial')
        """, (cutoff,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def remove(self, run_id):
        with self.db:
            self.db.execute("DELETE FROM runs WHERE id=?", (run_id,))
