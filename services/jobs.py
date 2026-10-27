"""Durable task receipts. A repeated request returns its existing task."""

import hashlib
import json
import sqlite3
import time
from contextlib import closing

from ..core.briefing import local_now, new_run_id
from .errors import PluginError


class JobStore:
    def __init__(self, root):
        self.path = root / "operations.sqlite3"

    def initialize(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT,created REAL,data TEXT)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS job_aliases(id TEXT PRIMARY KEY,target TEXT)")
            rows = db.execute("SELECT id,data FROM jobs").fetchall()
            for request_id, raw in rows:
                job = json.loads(raw)
                if job["status"] == "running":
                    job.update(
                        status="failed",
                        error_code="PROCESS_INTERRUPTED",
                        error_message="上次运行随插件进程中断；结果未确认，不会自动再次生成。",
                        finished=local_now().isoformat(),
                    )
                    db.execute(
                        "UPDATE jobs SET data=? WHERE id=?",
                        (json.dumps(job, ensure_ascii=False), request_id),
                    )
            self._prune_temporary(db)

    def _existing(self, db, operation, request_id, account_scope):
        row = db.execute(
            "SELECT kind,data FROM jobs WHERE id=COALESCE((SELECT target FROM job_aliases WHERE id=?),?)",
            (request_id, request_id),
        ).fetchone()
        if row is None:
            return None
        previous = json.loads(row[1])
        if row[0] != operation or previous.get("account_scope") != account_scope:
            raise PluginError("REQUEST_CONFLICT", status=409)
        return previous

    def _prune_temporary(self, db):
        for request_id, raw in db.execute(
            "SELECT id,data FROM jobs WHERE kind='temporary_search' AND created<? AND (data LIKE '%\"result\":%' OR data LIKE '%\"params\":%')",
            (time.time() - 86400,),
        ).fetchall():
            job = json.loads(raw)
            if job.get("status") != "running" and ("result" in job or "params" in job):
                job.pop("result", None)
                job.pop("params", None)
                job.update(results_expired=True, record_available=False)
                db.execute(
                    "UPDATE jobs SET data=? WHERE id=?",
                    (json.dumps(job, ensure_ascii=False), request_id),
                )

    def get(self, operation, request_id, account_scope):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            if operation == "temporary_search":
                self._prune_temporary(db)
            return self._existing(db, operation, request_id, account_scope)

    def reuse_collection(self, request_id, target, account_scope):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = self._existing(db, "collect", request_id, account_scope)
            if previous is not None:
                return previous
            job = self._existing(db, "collect", target, account_scope)
            if job is None or job["status"] != "running":
                raise PluginError("TASK_BUSY", status=409)
            db.execute("INSERT INTO job_aliases VALUES(?,?)", (request_id, job["id"]))
            return dict(job, reused_running=True)

    def start(self, operation, request_id, account_scope, *, payload=None, message_key=None):
        payload_hash = (
            hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            if payload is not None
            else None
        )
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if operation == "temporary_search":
                self._prune_temporary(db)
            previous = self._existing(db, operation, request_id, account_scope)
            if previous is not None:
                if payload_hash is not None and previous.get("payload_hash") != payload_hash:
                    raise PluginError("REQUEST_CONFLICT", status=409)
                return previous, False
            if operation == "temporary_search" and message_key:
                rows = db.execute(
                    "SELECT data FROM jobs WHERE kind='temporary_search' AND created>?",
                    (time.time() - 86400,),
                ).fetchall()
                if sum(json.loads(r[0]).get("message_key") == message_key for r in rows) >= 3:
                    raise PluginError("TEMP_SEARCH_LIMIT")
            for (raw,) in db.execute("SELECT data FROM jobs ORDER BY created DESC LIMIT 2"):
                if json.loads(raw)["status"] == "running":
                    raise PluginError("TASK_BUSY", status=409)
            now = local_now()
            job = {
                "id": request_id,
                "account_scope": account_scope,
                "status": "running",
                "operation": operation,
                "preview": operation == "collect",
                "started": now.isoformat(),
                "run_id": new_run_id(now),
                "record_available": False,
                "error": None,
            }
            if payload is not None:
                job.update(params=payload, payload_hash=payload_hash, message_key=message_key)
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?)",
                (request_id, operation, time.time(), json.dumps(job)),
            )
            return job, True

    def finish(self, job):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute(
                "UPDATE jobs SET data=? WHERE id=?",
                (json.dumps(job, ensure_ascii=False), job["id"]),
            )

    def latest(self, operation, account_scope):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            if operation == "temporary_search":
                self._prune_temporary(db)
            rows = db.execute(
                "SELECT data FROM jobs WHERE kind=? ORDER BY created DESC,rowid DESC",
                (operation,),
            )
            for row in rows:
                job = json.loads(row[0])
                if job.get("account_scope") == account_scope:
                    return job
            return None
