import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .models import CATEGORY, Example
from .schema import load_json


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, location, seeds):
        self.location = Path(location)
        self.location.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS objects (
                    kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY (kind, id)
                );
                CREATE TABLE IF NOT EXISTS example_history (
                    id TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(id, version)
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM objects WHERE kind='meta' AND id='seeded'").fetchone():
                for row in load_json(seeds):
                    data = Example.model_validate({k: v for k, v in row.items() if k != "id"})
                    self._example(db, data, row["id"], 1)
                self._save(db, "meta", "seeded", True)
            for (body,) in db.execute("SELECT body FROM objects WHERE kind='batch'"):
                batch = json.loads(body)
                active = False
                for item in batch["items"]:
                    if item["status"] in {"queued", "running"}:
                        item.update(status="interrupted", error="服务重启前任务未完成")
                        active = True
                if active:
                    batch["status"] = "interrupted"
                    self._save(db, "batch", batch["id"], batch)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.location, timeout=3)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _save(db, kind, id_, data):
        db.execute(
            "INSERT INTO objects(kind,id,body) VALUES (?,?,?) "
            "ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body",
            (kind, id_, pack(data)),
        )

    def put(self, kind, id_, data):
        with self.connect() as db:
            self._save(db, kind, id_, data)

    def get(self, kind, id_):
        with self.connect() as db:
            found = db.execute("SELECT body FROM objects WHERE kind=? AND id=?", (kind, id_)).fetchone()
            return json.loads(found[0]) if found else None

    def list(self, kind):
        with self.connect() as db:
            return [json.loads(body) for (body,) in
                    db.execute("SELECT body FROM objects WHERE kind=? ORDER BY id", (kind,))]

    def _example(self, db, data, id_, version):
        category = "属性过滤" if data.plan.operation == "list" and (data.plan.contains or data.plan.name) else CATEGORY[data.plan.operation]
        entry = {"id": id_, "version": version, "updated_at": now(),
                 **data.model_dump(), "category": category}
        self._save(db, "example", id_, entry)
        db.execute("INSERT INTO example_history VALUES (?,?,?)", (id_, version, pack(entry)))
        return entry

    def write_example(self, data, id_=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if id_ is None:
                return self._example(db, data, uuid4().hex, 1)
            row = db.execute("SELECT body FROM objects WHERE kind='example' AND id=?", (id_,)).fetchone()
            if row:
                return self._example(db, data, id_, json.loads(row[0])["version"] + 1)
            return None

    def delete_example(self, id_):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body FROM objects WHERE kind='example' AND id=?", (id_,)).fetchone()
            if not row:
                return False
            prior = json.loads(row[0])
            prior.update(version=prior["version"] + 1, deleted=True, updated_at=now())
            db.execute("DELETE FROM objects WHERE kind='example' AND id=?", (id_,))
            db.execute("INSERT INTO example_history VALUES (?,?,?)", (id_, prior["version"], pack(prior)))
            return True

    def history(self, id_):
        with self.connect() as db:
            return [json.loads(body) for (body,) in db.execute(
                "SELECT body FROM example_history WHERE id=? ORDER BY version DESC", (id_,))]
