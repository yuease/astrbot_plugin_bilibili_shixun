"""SQLite is authoritative; completed output is an idempotent file outbox."""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path


def dump(v):
    return json.dumps(v, ensure_ascii=False, indent=2)


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of death.
        try:
            code = ctypes.c_ulong()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Store:
    def __init__(self, path, output=None, clock=None, rebuild_preferences=True):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.output = Path(output or path.parent / "briefings")
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        has_runs = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='runs'").fetchone()
        self.migration_backup = None
        migrated_schema = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='context_migrations'"
        ).fetchone()
        pool_schema = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='pool_items'"
        ).fetchone()
        agent_schema = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='agent_requests'"
        ).fetchone()
        if has_runs and (not migrated_schema or not pool_schema or not agent_schema):
            # Complete online backup before any schema/data mutation; unique name avoids overwriting.
            self.migration_backup = path.with_name(
                path.name
                + (
                    ".pre-stable-"
                    if not migrated_schema
                    else ".pre-pool-"
                    if not pool_schema
                    else ".pre-agent-"
                )
                + uuid.uuid4().hex[:10]
                + ".sqlite3"
            )
            self.backup(self.migration_backup)
        self.db.executescript("""
        PRAGMA foreign_keys=ON;
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, context TEXT, created TEXT, status TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS candidates(run TEXT REFERENCES runs(id), id TEXT, data TEXT, PRIMARY KEY(run,id));
        CREATE TABLE IF NOT EXISTS aliases(alias TEXT PRIMARY KEY, video TEXT);
        CREATE TABLE IF NOT EXISTS recommendations(id TEXT PRIMARY KEY, run TEXT REFERENCES runs(id), video TEXT,
          position INTEGER, data TEXT, UNIQUE(run,video));
        CREATE TABLE IF NOT EXISTS feedback_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE,
          recommendation TEXT REFERENCES recommendations(id), value TEXT, reason TEXT, created TEXT,
          target TEXT, request_key TEXT UNIQUE, payload TEXT);
        CREATE TABLE IF NOT EXISTS preference_snapshots(version INTEGER PRIMARY KEY AUTOINCREMENT,
          context TEXT, created TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS preference_heads(context TEXT PRIMARY KEY, version INTEGER REFERENCES preference_snapshots(version));
        CREATE TABLE IF NOT EXISTS preference_evidence(version INTEGER REFERENCES preference_snapshots(version), feature TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS source_scans(namespace TEXT,kind TEXT,source_key TEXT,checked TEXT,data TEXT,PRIMARY KEY(namespace,kind,source_key));
        """)
        if "pinned" not in {r[1] for r in self.db.execute("PRAGMA table_info(preference_heads)")}:
            self.db.execute(
                "ALTER TABLE preference_heads ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0"
            )
            self.db.commit()
        from .migrations import migrate

        try:
            migrate(self.db)
            from .pool import migrate_pool

            migrate_pool(self.db)
            from .agent_migrations import migrate_agent

            migrate_agent(self.db)
            from .settings_service import recover_pending

            recover_pending(self)
        except Exception:
            self.db.close()
            raise
        # Rebuild every attributable old goal, not just the profile currently open in the UI.
        # Never combine old weight snapshots. Failure stays retryable at next open.
        from .preferences import SHANGHAI, refresh_safe
        from .schemas import Profile

        self.migration_rebuilds = []
        targets = (
            self.db.execute("""SELECT DISTINCT m.stable_context FROM context_migrations m
            LEFT JOIN preference_heads h ON h.context=m.stable_context WHERE h.context IS NULL
            AND m.original_context!=m.stable_context""").fetchall()
            if rebuild_preferences
            else []
        )
        for target in targets:
            row = self.db.execute(
                "SELECT data FROM runs WHERE context=? LIMIT 1", (target[0],)
            ).fetchone()
            profile = Profile.model_validate(json.loads(row[0])["profile"])
            now = clock() if clock else datetime.now(SHANGHAI)
            version, _, error = refresh_safe(self, profile, now)
            self.migration_rebuilds.append(dict(context=target[0], version=version, error=error))

    def close(self):
        self.db.close()

    def canonicalize(self, article):
        aliases = article["source_ids"]
        matches = {
            r[0]
            for a in aliases
            for r in self.db.execute("SELECT video FROM aliases WHERE alias=?", (a,))
        }
        if len(matches) > 1:
            article["parse_warnings"].append("conflicting aliases; not merged")
            return article
        article["id"] = next(iter(matches), article["id"])
        with self.db:
            for a in aliases:
                self.db.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (a, article["id"]))
        return article

    def run(self, run_id, context, now, status, data, articles=None, picks=None):
        with self.db:
            self.db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
                (run_id, context, now.isoformat(), status, dump(data)),
            )
            from .agent_migrations import attribute_run

            attribute_run(self.db, run_id, context, now.isoformat(), data)
            if articles is not None:
                self.db.executemany(
                    "INSERT OR REPLACE INTO candidates VALUES (?,?,?)",
                    [(run_id, a["id"], dump(a)) for a in articles],
                )
            if picks is not None:
                by_id = {a["id"]: a for a in articles}
                for i, p in enumerate(picks, 1):
                    snapshot = dict(p, article=by_id[p["id"]])
                    self.db.execute(
                        "INSERT OR IGNORE INTO recommendations VALUES (?,?,?,?,?)",
                        (run_id + ":" + str(i), run_id, p["id"], i, dump(snapshot)),
                    )

    def belongs(self, run_id, profile):
        row = self.db.execute("SELECT context FROM runs WHERE id=?", (run_id,)).fetchone()
        return bool(row and row[0] == profile.context)

    def record_source_check(
        self, account_scope, ref, status, now, *, profile=None, count=0, error_code=None
    ):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO source_checks VALUES (?,?,?,?,?)",
                (account_scope, ref["kind"], ref["key"], status, now.isoformat()),
            )
            if profile is not None:
                from .agent_migrations import observation
                from .pool import namespace

                observation(self.db, namespace(profile), ref, status, now, count, error_code)

    def source_status(self, profile):
        source = profile.source_plan.following
        kind = "following_videos" if source.scope == "videos" else "following_dynamics"
        row = self.db.execute(
            "SELECT status,checked FROM source_checks WHERE account_scope=? AND kind=? AND key=?",
            (profile.account_scope, kind, source.uid),
        ).fetchone()
        statuses = {
            "ok": "ready",
            "empty": "ready",
            "auth_required": "missing_credentials",
            "auth_expired": "auth_expired",
            "failed": "error",
        }
        return [
            dict(
                kind=kind,
                key=source.uid,
                status="disabled"
                if not source.enabled
                else statuses.get(row["status"], "unknown")
                if row
                else "unknown",
                last_checked=row["checked"] if row else None,
            )
        ]

    def export(self, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        data = json.loads(row["data"])
        from .errors import public_diagnostic, source_public

        safe_error = public_diagnostic(data)
        for key in ("error_code", "error_message", "error_details"):
            data.pop(key, None)
        data.update(safe_error)
        if "source_results" in data:
            data["source_results"] = [source_public(x) for x in data["source_results"]]
        recovered = data.get("export_failed") is True
        if recovered:
            data.update(export_failed=False, export_recovered=True)
            data.update(public_diagnostic(data))
        directory = self.output / run_id
        atomic_write(
            directory / "candidates.json",
            dump(
                [
                    json.loads(r[0])
                    for r in self.db.execute(
                        "SELECT data FROM candidates WHERE run=? ORDER BY rowid", (run_id,)
                    )
                ]
            ),
        )
        atomic_write(directory / "run.json", dump(dict(data, status=row["status"])))
        if row["status"] in ("completed", "empty") and "report" in data:
            atomic_write(directory / "briefing.md", data["report"])
        if recovered:
            # Only clear the outstanding warning after every output has been written.
            with self.db:
                self.db.execute("UPDATE runs SET data=? WHERE id=?", (dump(data), run_id))

    def recover(self):
        # Only committed runs are published. In-flight runs are never promoted.
        for row in self.db.execute(
            "SELECT * FROM runs WHERE status IN ('started','collected')"
        ).fetchall():
            data = json.loads(row["data"])
            if not process_alive(data.get("process_id")):
                from .errors import diagnostic

                data.update(
                    failed_stage="interrupted",
                    error_type="ProcessInterrupted",
                    **diagnostic("PROCESS_INTERRUPTED"),
                )
                if data.get("metrics", {}).get("logical_model_calls"):
                    data["metrics"].update(http_attempts=None, network_retries=None)
                with self.db:
                    self.db.execute(
                        "UPDATE runs SET status='failed', data=? WHERE id=?",
                        (dump(data), row["id"]),
                    )
        for row in self.db.execute(
            "SELECT id FROM runs WHERE status IN ('completed','empty','preview','failed','collection')"
        ).fetchall():
            self.export(row["id"])

    def effective_feedback(self, context):
        rows = self.db.execute(
            """SELECT f.*, r.video, r.data AS snapshot FROM feedback_events f
          JOIN recommendations r ON r.id=f.recommendation JOIN runs u ON u.id=r.run
          WHERE u.context=? ORDER BY f.seq""",
            (context,),
        ).fetchall()
        undone = {r["target"] for r in rows if r["value"] == "undo"}
        current = {}
        for r in rows:
            if r["value"] != "undo" and r["id"] not in undone:
                current[r["recommendation"]] = dict(r)
        return list(current.values())

    def blocked(self, context, now, profile):
        cutoff = (now - timedelta(days=profile.history_cooldown_days)).isoformat()
        ids = {
            r[0]
            for r in self.db.execute(
                """SELECT r.video FROM recommendations r JOIN runs u ON u.id=r.run
            WHERE u.context=? AND u.status='completed' AND u.created>?""",
                (context, cutoff),
            )
        }
        for f in self.effective_feedback(context):
            if (
                f["value"] == "later"
                and datetime.fromisoformat(f["created"]) + timedelta(days=profile.snooze_days) > now
            ):
                ids.add(f["video"])
        return ids

    def feedback(self, recommendation, value, reason, now, key=None, undo=None):
        if value not in ("useful", "not_useful", "later", "undo"):
            raise ValueError("invalid feedback")
        if undo:
            row = self.db.execute(
                "SELECT * FROM feedback_events WHERE id=? AND value!='undo'", (undo,)
            ).fetchone()
            if not row:
                raise ValueError("unknown event")
            recommendation = row["recommendation"]
        payload = dump([recommendation, value, reason, undo])
        with self.db:
            if key:
                old = self.db.execute(
                    "SELECT id,payload FROM feedback_events WHERE request_key=?", (key,)
                ).fetchone()
                if old:
                    if old["payload"] != payload:
                        raise ValueError("idempotency key reused with different feedback")
                    return old["id"]
            if not self.db.execute(
                "SELECT 1 FROM recommendations WHERE id=?", (recommendation,)
            ).fetchone():
                raise ValueError("unknown recommendation")
            event = uuid.uuid4().hex
            self.db.execute(
                "INSERT INTO feedback_events(id,recommendation,value,reason,created,target,request_key,payload) VALUES (?,?,?,?,?,?,?,?)",
                (event, recommendation, value, reason, now.isoformat(), undo, key, payload),
            )
            self.db.execute(
                """UPDATE preference_heads SET pinned=0 WHERE context IN
                (SELECT u.context FROM runs u JOIN recommendations r ON r.run=u.id WHERE r.id=?)""",
                (recommendation,),
            )
        return event

    def preferences(self, context):
        row = self.db.execute(
            """SELECT s.version,s.data FROM preference_snapshots s JOIN preference_heads h
            ON h.version=s.version WHERE h.context=?""",
            (context,),
        ).fetchone()
        return (row["version"], json.loads(row["data"])) if row else (None, {})

    def feedback_stats(self, context):
        total = self.db.execute(
            """SELECT count(*) FROM recommendations r JOIN runs u ON u.id=r.run
            WHERE u.context=?""",
            (context,),
        ).fetchone()[0]
        values = [f["value"] for f in self.effective_feedback(context)]
        rated = sum(v in ("useful", "not_useful") for v in values)
        return dict(
            recommendations=total,
            feedback_count=len(values),
            evaluated=rated,
            coverage=len(values) / total if total else None,
            useful_rate=values.count("useful") / rated if rated else None,
        )

    def save_preferences(self, context, now, weights, evidence):
        with self.db:
            cur = self.db.execute(
                "INSERT INTO preference_snapshots(context,created,data) VALUES (?,?,?)",
                (context, now.isoformat(), dump(weights)),
            )
            version = cur.lastrowid
            self.db.executemany(
                "INSERT INTO preference_evidence VALUES (?,?,?)",
                [(version, k, dump(v)) for k, v in evidence.items()],
            )
            self.db.execute(
                "INSERT OR REPLACE INTO preference_heads(context,version,pinned) VALUES (?,?,0)",
                (context, version),
            )
        return version, weights

    def restore_preferences(self, context, version):
        with self.db:
            if not self.db.execute(
                "SELECT 1 FROM preference_snapshots WHERE context=? AND version=?",
                (context, version),
            ).fetchone():
                raise ValueError("unknown preference version")
            self.db.execute(
                "INSERT OR REPLACE INTO preference_heads(context,version,pinned) VALUES (?,?,1)",
                (context, version),
            )

    def backup(self, destination):
        destination = Path(destination).resolve()
        if destination.exists():
            raise ValueError("backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination) as target:
            self.db.backup(target)
