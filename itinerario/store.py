import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from .models import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, created TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'queued', payload TEXT,
    delivery TEXT, attempts INTEGER NOT NULL DEFAULT 0, retry_after TEXT
);
CREATE INDEX IF NOT EXISTS jobs_stage ON jobs(stage, id);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, created TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS message_history ON messages(owner, id);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY, owner INTEGER NOT NULL, created TEXT NOT NULL, title TEXT NOT NULL,
    data TEXT NOT NULL, pdf BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS report_history ON reports(owner, created);
CREATE TABLE IF NOT EXISTS active_report (
    owner INTEGER PRIMARY KEY, report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS quotas (
    owner INTEGER NOT NULL, day TEXT NOT NULL, category TEXT NOT NULL, used INTEGER NOT NULL,
    PRIMARY KEY(owner, day, category)
);
"""


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "itinerario.sqlite3", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def offset(self, today: str) -> int | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = 'offset'").fetchone()
        if row:
            offset, stamp = json.loads(row["value"])
            if date.fromisoformat(today) - date.fromisoformat(stamp) < timedelta(days=6):
                return offset
        return None

    def enqueue(self, updates: list[dict], now: str, allowed: frozenset[int]):
        if not updates:
            return
        with self.db:
            for update in updates:
                message = update.get("message") or {}
                owner = message.get("from", {}).get("id")
                chat = message.get("chat", {})
                if chat.get("type") != "private" or owner is None or chat.get("id") != owner:
                    continue
                if owner not in allowed and (message.get("text") or "").split("@")[0].strip() != "/miid":
                    continue
                self.db.execute(
                    "INSERT OR IGNORE INTO jobs(id, owner, created, payload) VALUES (?, ?, ?, ?)",
                    (update["update_id"], owner, now, json.dumps(message, ensure_ascii=False)),
                )
            self.db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('offset', ?)",
                (json.dumps((max(update["update_id"] for update in updates) + 1, now[:10])),),
            )

    def recover(self):
        with self.db:
            self.db.execute(
                "UPDATE jobs SET stage='delivery', payload=NULL, delivery=? WHERE stage='working'",
                (json.dumps({"text": "El proceso se interrumpió. Reenviá el pedido si querés volver a intentarlo."}),),
            )

    def next_job(self):
        with self.db:
            row = self.db.execute(
                "UPDATE jobs SET stage='working' WHERE id = "
                "(SELECT id FROM jobs WHERE stage='queued' ORDER BY id LIMIT 1) "
                "AND stage='queued' RETURNING *"
            ).fetchone()
            return row

    def next_delivery(self, now: str):
        return self.db.execute(
            "SELECT * FROM jobs WHERE stage='delivery' AND (retry_after IS NULL OR retry_after <= ?) "
            "ORDER BY id LIMIT 1", (now,),
        ).fetchone()

    def deliver(self, job_id: int, delivery: dict, assistant_text: str = "", now: str = "",
                report: tuple[Snapshot, bytes] | None = None):
        with self.db:
            owner_row = self.db.execute("SELECT owner FROM jobs WHERE id=?", (job_id,)).fetchone()
            owner = owner_row["owner"]
            if report:
                snapshot, pdf = report
                report_id = uuid4().hex[:12]
                self.db.execute(
                    "INSERT INTO reports VALUES (?, ?, ?, ?, ?, ?)",
                    (report_id, owner, snapshot.created, snapshot.report.title, snapshot.model_dump_json(), pdf),
                )
                self._select_report(owner, report_id)
                delivery["report_id"] = report_id
            if assistant_text:
                self.db.execute("INSERT INTO messages(owner, created, role, text) VALUES (?, ?, ?, ?)",
                                (owner, now, "assistant", assistant_text))
            self.db.execute("UPDATE jobs SET stage='delivery', payload=NULL, delivery=? WHERE id=?",
                            (json.dumps(delivery, ensure_ascii=False), job_id))

    def finish(self, job_id: int):
        with self.db:
            self.db.execute("UPDATE jobs SET stage='done', delivery=NULL, payload=NULL WHERE id=?", (job_id,))

    def retry_delivery(self, job_id: int, retry_after: str):
        with self.db:
            self.db.execute("UPDATE jobs SET attempts=attempts+1, retry_after=? WHERE id=?", (retry_after, job_id))

    def history(self, owner: int, limit: int = 10):
        return list(reversed(self.db.execute(
            "SELECT role, text FROM messages WHERE owner=? ORDER BY id DESC LIMIT ?", (owner, limit),
        ).fetchall()))

    def remember(self, owner: int, now: str, text: str):
        with self.db:
            self.db.execute("INSERT INTO messages(owner, created, role, text) VALUES (?, ?, ?, ?)",
                            (owner, now, "user", text))

    def list_reports(self, owner: int, limit: int = 12):
        return self.db.execute(
            "SELECT id, title, created FROM reports WHERE owner=? ORDER BY created DESC, rowid DESC LIMIT ?",
            (owner, limit),
        ).fetchall()

    def _select_report(self, owner: int, report_id: str):
        self.db.execute("INSERT OR REPLACE INTO active_report VALUES (?, ?)", (owner, report_id))

    def select_report(self, owner: int, report_id: str):
        with self.db:
            row = self.report(owner, report_id)
            if row:
                self._select_report(owner, report_id)
            return row

    def report(self, owner: int, report_id: str | None = None):
        if report_id is None:
            return self.db.execute(
                "SELECT reports.* FROM reports JOIN active_report ON active_report.report_id=reports.id "
                "WHERE reports.owner=? AND active_report.owner=?", (owner, owner),
            ).fetchone()
        return self.db.execute("SELECT * FROM reports WHERE owner=? AND id=?", (owner, report_id)).fetchone()

    def purge_owner(self, owner: int, keep_job: int):
        with self.db:
            self.db.execute("DELETE FROM messages WHERE owner=?", (owner,))
            self.db.execute("DELETE FROM reports WHERE owner=?", (owner,))
            self.db.execute(
                "UPDATE jobs SET stage='done', payload=NULL, delivery=NULL WHERE owner=? AND id<?",
                (owner, keep_job),
            )

    def prune(self, cutoff: str, job_cutoff: str):
        with self.db:
            self.db.execute("DELETE FROM messages WHERE created < ?", (cutoff,))
            self.db.execute("DELETE FROM reports WHERE created < ?", (cutoff,))
            self.db.execute("DELETE FROM jobs WHERE stage='done' AND created < ?", (job_cutoff,))
            self.db.execute("DELETE FROM quotas WHERE day < ?", (job_cutoff[:10],))

    def reserve(self, day: str, category: str, limits: dict[int, int]) -> bool:
        with self.db:
            self.db.execute("SAVEPOINT reserve_quota")
            for owner, limit in limits.items():
                row = self.db.execute(
                    "INSERT INTO quotas VALUES (?, ?, ?, 1) ON CONFLICT(owner, day, category) "
                    "DO UPDATE SET used=used+1 WHERE used < ? RETURNING used",
                    (owner, day, category, limit),
                ).fetchone()
                if row is None:
                    self.db.execute("ROLLBACK TO reserve_quota")
                    self.db.execute("RELEASE reserve_quota")
                    return False
            self.db.execute("RELEASE reserve_quota")
            return True
