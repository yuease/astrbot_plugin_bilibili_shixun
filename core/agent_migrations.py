"""Schema v4: add private query/settings ledgers without rewriting legacy feedback or contexts."""

import json
from datetime import datetime, timezone

from .errors import MESSAGES
from .schemas import Profile


def goal_id(profile):
    return (
        profile.interest_identity.goal_id
        if profile.interest_identity
        else "legacy-" + profile.context
    )


def utc(value):
    try:
        d = datetime.fromisoformat(value)
        return d.astimezone(timezone.utc).isoformat() if d.tzinfo else None
    except (ValueError, TypeError):
        return None


def observation(db, namespace, ref, status, now, count=0, error_code=None):
    stamp = utc(now.isoformat() if hasattr(now, "isoformat") else now)
    if not stamp or status == "pending" or error_code == "SOURCE_BUDGET":
        return
    previous = db.execute(
        "SELECT * FROM source_observations WHERE namespace=? AND kind=? AND source_key=?",
        (namespace, ref["kind"], ref["key"]),
    ).fetchone()
    success = stamp if status in ("ok", "empty") else previous["last_success"] if previous else None
    code = (
        error_code
        if error_code in MESSAGES
        else "LEGACY_UNKNOWN"
        if status not in ("ok", "empty")
        else None
    )
    if previous and previous["checked"] > stamp:
        return
    db.execute(
        "INSERT OR REPLACE INTO source_observations VALUES(?,?,?,?,?,?,?,?)",
        (
            namespace,
            ref["kind"],
            ref["key"],
            stamp,
            success,
            status,
            count if type(count) is int and count >= 0 else 0,
            code,
        ),
    )


def attribute_run(db, run_id, context, created, data):
    gid = None
    reason = "unattributed_missing_or_incompatible_profile"
    try:
        p = Profile.model_validate(data["profile"])
        if context == p.context:
            gid = goal_id(p)
            reason = "exact_context_preserved"
            db.execute(
                "INSERT OR IGNORE INTO interest_goals VALUES(?,?,?,?,?)",
                (p.profile_id, gid, p.context, created, reason),
            )
            db.execute(
                "INSERT OR IGNORE INTO interest_revisions VALUES(?,?,?,?,?,?)",
                ("run-" + run_id, p.profile_id, gid, p.context, p.interests, created),
            )
    except (ValueError, KeyError, TypeError):
        pass
    db.execute(
        "INSERT OR IGNORE INTO interest_attributions VALUES(?,?,?,?)",
        (run_id, gid, context, reason),
    )


def migrate_agent(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_requests'").fetchone():
        if db.execute("PRAGMA user_version").fetchone()[0] < 4:
            db.execute("PRAGMA user_version=4")
            db.commit()
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS agent_requests(namespace TEXT,request_key TEXT,payload_hash TEXT,status TEXT,pid INTEGER,created TEXT,context_hash TEXT,data TEXT,PRIMARY KEY(namespace,request_key))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS agent_cache(key TEXT PRIMARY KEY,created REAL,expires REAL,data TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS temporary_items(namespace TEXT,keyword TEXT,entry_key TEXT,id TEXT,first_seen TEXT,last_seen TEXT,query_id TEXT,data TEXT,PRIMARY KEY(namespace,keyword,entry_key))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS temporary_origins(namespace TEXT,query_id TEXT,keyword TEXT,entry_key TEXT,fetched TEXT,PRIMARY KEY(namespace,query_id,keyword,entry_key))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS source_observations(namespace TEXT,kind TEXT,source_key TEXT,checked TEXT,last_success TEXT,status TEXT,count INTEGER,error_code TEXT,PRIMARY KEY(namespace,kind,source_key))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS setting_changes(id TEXT PRIMARY KEY,namespace TEXT,request_key TEXT,payload_hash TEXT,created TEXT,status TEXT,profile_path TEXT,before_text TEXT,after_text TEXT,before_version TEXT,after_version TEXT,fields TEXT,before_heads TEXT,response TEXT,undo_of TEXT,UNIQUE(namespace,request_key),UNIQUE(namespace,undo_of))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS setting_heads(namespace TEXT,field TEXT,change_id TEXT,PRIMARY KEY(namespace,field))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS interest_goals(profile_id TEXT,goal_id TEXT,context TEXT,created TEXT,reason TEXT,PRIMARY KEY(profile_id,goal_id,context))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS interest_revisions(id TEXT PRIMARY KEY,profile_id TEXT,goal_id TEXT,context TEXT,interests TEXT,created TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS interest_attributions(run TEXT PRIMARY KEY,goal_id TEXT,context TEXT,reason TEXT)"
        )
        from .pool import namespace

        for row in db.execute("SELECT * FROM runs ORDER BY created,rowid").fetchall():
            gid = None
            reason = "unattributed_missing_or_incompatible_profile"
            try:
                data = json.loads(row["data"])
                p = Profile.model_validate(data["profile"])
                if row["context"] == p.context:
                    gid = goal_id(p)
                    reason = "exact_context_preserved"
                    db.execute(
                        "INSERT OR IGNORE INTO interest_goals VALUES(?,?,?,?,?)",
                        (p.profile_id, gid, p.context, row["created"], reason),
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO interest_revisions VALUES(?,?,?,?,?,?)",
                        (
                            "run-" + row["id"],
                            p.profile_id,
                            gid,
                            p.context,
                            p.interests,
                            row["created"],
                        ),
                    )
                for ref in data.get("source_results", []):
                    observation(
                        db,
                        namespace(p),
                        ref,
                        ref.get("status", "failed"),
                        row["created"],
                        ref.get("count", 0),
                        ref.get("error_code"),
                    )
            except (ValueError, KeyError, TypeError):
                pass
            db.execute(
                "INSERT OR IGNORE INTO interest_attributions VALUES(?,?,?,?)",
                (row["id"], gid, row["context"], reason),
            )
        db.execute("PRAGMA user_version=4")
        db.commit()
    except Exception:
        db.rollback()
        raise
