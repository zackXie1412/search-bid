from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .bid_models import BidCheckError
from .bid_review import REVIEW_SCOPE_VERSION, decision_view, profile_warnings
from .bid_skills import screening_metadata
from .bid_screening import build_evidence_index, screening_display
from .notice_query import NoticeQueryParams, notice_conditions


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BidTaskStore:
    def __init__(self, runtime: Path):
        self.root = runtime / "bid_check"
        self.path = self.root / "tasks.sqlite3"

    @contextmanager
    def connect(self):
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        try:
            with closing(sqlite3.connect(self.path, timeout=5)) as db:
                os.chmod(self.path, 0o600)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                with db:
                    yield db
        except sqlite3.Error:
            raise BidCheckError("本机检标任务存储暂时不可用，请重试") from None

    def initialize(self):
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, status TEXT NOT NULL, filters TEXT NOT NULL,
              files TEXT NOT NULL, model_config TEXT NOT NULL, profile TEXT,
              sources TEXT, error TEXT NOT NULL DEFAULT '', stop_requested INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
              task_id TEXT NOT NULL REFERENCES tasks(id), position INTEGER NOT NULL,
              notice TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
              decision TEXT, error TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(task_id,position)
            );
            CREATE INDEX IF NOT EXISTS idx_bid_items_status ON items(task_id,status,position);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
            if "diagnostic" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN diagnostic TEXT NOT NULL DEFAULT '{}'")
            if "knowledge_revision_id" not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN knowledge_revision_id TEXT")
            if 'knowledge_context' not in columns:
                db.execute("ALTER TABLE tasks ADD COLUMN knowledge_context TEXT NOT NULL DEFAULT '{}'")

            for name in ('rules_snapshot', 'evidence_index'):
                if name not in columns:
                    db.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT")
            item_columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
            if 'notice_snapshot' not in item_columns:
                db.execute("ALTER TABLE items ADD COLUMN notice_snapshot TEXT")

    def create(self, database: Path, filters: NoticeQueryParams, files: list[dict], config: dict, task_id: str, *, snapshot=None, knowledge_revision_id=None, rules_snapshot=None):
        conditions, values = notice_conditions(filters)
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as source:
                source.row_factory = sqlite3.Row
                source.execute("BEGIN")
                rows = source.execute(
                    "SELECT rowid AS id,title,canonical_url,published_at,region_code,region_raw FROM pending_notices WHERE "
                    + " AND ".join(conditions) + " ORDER BY published_at DESC,rowid DESC", values,
                ).fetchall()
        except (sqlite3.Error, OSError):
            raise BidCheckError("无法读取本地公告，检标任务未创建") from None
        if not rows:
            raise BidCheckError("当前筛选没有匹配公告，请重新查询")
        timestamp = now()
        with self.connect() as db:
            db.execute("INSERT INTO tasks(id,status,filters,files,model_config,created_at,updated_at) VALUES (?,?,?,?,?,?,?)", (
                task_id, "draft", filters.model_dump_json(exclude={"limit"}), json.dumps(files, ensure_ascii=False),
                json.dumps(config, ensure_ascii=False), timestamp, timestamp,
            ))
            db.executemany("INSERT INTO items(task_id,position,notice) VALUES (?,?,?)", (
                (task_id, index, json.dumps(dict(row), ensure_ascii=False)) for index, row in enumerate(rows)
            ))
            if rules_snapshot:
                db.execute('UPDATE tasks SET rules_snapshot=? WHERE id=?', (json.dumps(rules_snapshot, ensure_ascii=False),task_id))
            if snapshot:
                index = build_evidence_index(snapshot['profile'], snapshot['sources'], snapshot.get('records', []))
                db.execute('UPDATE tasks SET evidence_index=? WHERE id=?', (json.dumps(index, ensure_ascii=False),task_id))
                db.execute('UPDATE tasks SET profile=?,sources=?,knowledge_revision_id=? WHERE id=?', (json.dumps(snapshot['profile'],ensure_ascii=False), json.dumps(snapshot['sources'],ensure_ascii=False), knowledge_revision_id,task_id))
                db.execute('UPDATE tasks SET knowledge_context=? WHERE id=?',(json.dumps({k:snapshot.get(k,[]) for k in ('material_support','coverage')},ensure_ascii=False),task_id))
        return self.detail(task_id)

    def raw(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        result = dict(row)
        for field in ("filters", "files", "model_config", "profile", "sources", "diagnostic", "knowledge_context", "rules_snapshot", "evidence_index"):
            if result[field] is not None:
                result[field] = json.loads(result[field])
        return result

    def update(self, task_id: str, **fields):
        allowed = {"status", "profile", "sources", "error", "stop_requested", "diagnostic", "evidence_index"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid task update")
        fields["updated_at"] = now()
        with self.connect() as db:
            db.execute("UPDATE tasks SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), task_id))

    def transition(self, task_id: str, allowed: set[str], target: str, *, retry=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,profile,files FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["status"] not in allowed:
                raise BidCheckError("任务状态已改变，请刷新后重试")
            if retry:
                if not row["profile"]:
                    if row["status"] != "failed" or not self.files_available(task_id, json.loads(row["files"])):
                        raise BidCheckError("原始上传资料已清理，请重新上传并创建任务")
                else:
                    count = db.execute("SELECT count(*) FROM items WHERE task_id=? AND status='failed'", (task_id,)).fetchone()[0]
                    if not count:
                        raise BidCheckError("没有失败条目可重试")
                db.execute("UPDATE items SET status='pending',error='' WHERE task_id=? AND status='failed'", (task_id,))
            db.execute("UPDATE tasks SET status=?,stop_requested=0,error='',diagnostic='{}',updated_at=? WHERE id=?", (target, now(), task_id))

    def files_available(self, task_id: str, files: list[dict]) -> bool:
        return bool(files) and all((self.root / task_id / file["stored_name"]).is_file() for file in files)

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE items SET status='pending' WHERE status='running'")
            db.execute("UPDATE tasks SET status='queued' WHERE status IN ('running','preparing') AND stop_requested=0")
            db.execute("UPDATE tasks SET status='stopped' WHERE stop_requested=1")

    def request_stop(self, task_id: str) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            immediate = row[0] in {"draft", "queued", "failed", "stopped"}
            if row[0] in {"draft", "queued", "preparing", "running", "failed", "stopped"}:
                db.execute("UPDATE tasks SET stop_requested=1,status=?,updated_at=? WHERE id=?", ("stopped" if immediate else "stopping", now(), task_id))
            return immediate

    def queued(self) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        return row[0] if row else None

    def claim(self, task_id: str) -> bool:
        with self.connect() as db:
            cursor = db.execute("UPDATE tasks SET status='preparing',updated_at=? WHERE id=? AND status='queued' AND stop_requested=0", (now(), task_id))
            return cursor.rowcount == 1

    def next_item(self, task_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM items WHERE task_id=? AND status='pending' ORDER BY position LIMIT 1", (task_id,)).fetchone()
        if not row:
            return None
        return {**dict(row), "notice": json.loads(row["notice"]), "notice_snapshot": json.loads(row['notice_snapshot']) if row['notice_snapshot'] else None}

    def save_notice_snapshot(self, task_id, position, snapshot):
        with self.connect() as db:
            db.execute('UPDATE items SET notice_snapshot=? WHERE task_id=? AND position=? AND notice_snapshot IS NULL', (json.dumps(snapshot, ensure_ascii=False), task_id, position))

    def item_update(self, task_id: str, position: int, status: str, *, decision=None, error=""):
        with self.connect() as db:
            db.execute("UPDATE items SET status=?,decision=?,error=?,attempts=attempts+? WHERE task_id=? AND position=?", (
                status, json.dumps(decision, ensure_ascii=False) if decision else None, error,
                1 if status == "running" else 0, task_id, position,
            ))
            db.execute("UPDATE tasks SET updated_at=? WHERE id=?", (now(), task_id))

    def list_tasks(self, limit=20) -> list[dict]:
        with self.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM tasks ORDER BY created_at DESC,rowid DESC LIMIT ?", (limit,))]
        return [self.detail(task_id, page_size=1, include_results=False) for task_id in ids]

    def detail(self, task_id: str, page=1, page_size=20, *, include_results=True) -> dict:
        raw = self.raw(task_id)
        with self.connect() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT status,count(*) FROM items WHERE task_id=? GROUP BY status", (task_id,))}
            current = db.execute("SELECT notice FROM items WHERE task_id=? AND status='running' LIMIT 1", (task_id,)).fetchone()
            rows = db.execute("SELECT notice,decision,notice_snapshot FROM items WHERE task_id=? AND status IN ('retain','needs_review','exclude') ORDER BY position", (task_id,)).fetchall()
            failed = db.execute("SELECT error,count(*) AS n FROM items WHERE task_id=? AND status='failed' GROUP BY error", (task_id,)).fetchall()
            legacy = db.execute("SELECT 1 FROM items WHERE task_id=? AND decision IS NOT NULL AND COALESCE(json_extract(decision,'$.review_scope_version'),0)<? LIMIT 1", (task_id, REVIEW_SCOPE_VERSION)).fetchone()
        derived = [{'notice':json.loads(row[0]),'decision':decision_view(screening_display(json.loads(row[1]),json.loads(row[2]) if row[2] else None,raw.get('evidence_index')))} for row in rows]
        visible = [item for item in derived if item['decision']['verdict']!='exclude']
        counts.update({status:sum(item['decision']['verdict']==status for item in derived) for status in ('retain','needs_review','exclude')})
        total = sum(counts.values())
        completed = sum(counts.get(status, 0) for status in ("retain", "exclude", "needs_review", "failed"))
        return {
            "id": task_id, "status": raw["status"], "filters": raw["filters"],
            "screening_rules": screening_metadata(raw.get('rules_snapshot')),
            "legacy_screening": not bool(raw.get('rules_snapshot')),
            "model_config": raw["model_config"], "created_at": raw["created_at"], "updated_at": raw["updated_at"],
            "file_names": [file["name"] for file in raw["files"]], "error": raw["error"],
            "total": total, "completed": completed, "retained": counts.get("retain", 0) + counts.get("needs_review", 0),
            "needs_review": counts.get("needs_review", 0), "excluded": counts.get("exclude", 0), "failed": counts.get("failed", 0),
            "estimated_calls": total + (0 if raw['knowledge_revision_id'] else 1), "has_profile": bool(raw["profile"]),
            "knowledge_revision_id": raw['knowledge_revision_id'],
            "knowledge_context":raw['knowledge_context'],
            "profile_warnings": profile_warnings(raw['profile'], raw['sources']) if include_results else [],
            "has_legacy_decisions": bool(legacy),
            "can_retry_preparation": raw["status"] == "failed" and not raw["profile"] and self.files_available(task_id, raw["files"]),
            "diagnostic": raw["diagnostic"],
            "current_title": json.loads(current[0])["title"] if current else "",
            "failures": [{"message": row[0], "count": row[1]} for row in failed],
            "items": visible[(page-1)*page_size:page*page_size] if include_results else [],
            "page": page, "page_size": page_size,
            "has_next": counts.get("retain", 0) + counts.get("needs_review", 0) > page * page_size,
        }
