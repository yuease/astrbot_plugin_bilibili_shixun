"""Persistent per-source contributions; current policy is applied when taking a snapshot."""

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta

from .filtering import RULES, block_reason, marks
from .schemas import Article
from .sources import prepare, subscriptions


def namespace(profile):
    return hashlib.sha256(
        json.dumps([profile.profile_id, profile.account_scope]).encode()
    ).hexdigest()


def entry_key(article):
    return (
        "dynamic:" + article["dynamic_id"]
        if article.get("is_dynamic") and article.get("dynamic_id")
        else article["id"]
    )


def upsert(db, profile, ref, article, now):
    a = Article.model_validate(article).model_dump()
    if ref["kind"] in ("up_dynamics", "following_dynamics", "following_videos"):
        a["is_dynamic"] = True
    a["sources"] = [{k: ref[k] for k in ("kind", "key", "label")}]
    a["filter_hits"] = marks(a)
    key = entry_key(a)
    space = namespace(profile)
    previous = db.execute(
        "SELECT first_seen,data FROM pool_items WHERE namespace=? AND kind=? AND source_key=? AND entry_key=?",
        (space, ref["kind"], ref["key"], key),
    ).fetchone()
    first = previous["first_seen"] if previous else now.isoformat()
    if previous:
        old = json.loads(previous["data"])
        # Re-fetching a dynamic never renews its publication clock, even if upstream changes it.
        if a.get("is_dynamic"):
            a["published_at"] = old.get("published_at")
            a["published"] = old.get("published", "未知")
    db.execute(
        """INSERT INTO pool_items(namespace,kind,source_key,entry_key,video,first_seen,last_seen,data)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(namespace,kind,source_key,entry_key) DO UPDATE SET
        video=excluded.video,last_seen=excluded.last_seen,data=excluded.data""",
        (
            space,
            ref["kind"],
            ref["key"],
            key,
            a["id"],
            first,
            now.isoformat(),
            json.dumps(a, ensure_ascii=False),
        ),
    )


def parse_time(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else None
    except (ValueError, TypeError):
        return None


def alive(article, first_seen, profile, now):
    if not article.get("is_dynamic"):
        return True
    published = parse_time(article.get("published_at"))
    if published is None or published > now + timedelta(
        seconds=profile.collection.future_tolerance_seconds
    ):
        anchor = parse_time(first_seen)
        article["expiry_basis"] = "first_seen_missing_or_invalid_publication"
    else:
        anchor = published
        article["expiry_basis"] = "publication_time"
    return bool(anchor and now < anchor + timedelta(hours=profile.collection.dynamic_ttl_hours))


def accepted_type(ref, article, profile):
    if ref["kind"] == "up_dynamics":
        wanted = next(u.content_type for u in profile.source_plan.ups if u.uid == ref["key"])
    elif ref["kind"] == "following_dynamics":
        wanted = profile.source_plan.following.content_type
    else:
        return True
    kind = article.get("content_type", "unknown")
    return (
        wanted == "all"
        or kind == "video"
        and wanted == "video"
        or kind not in ("video", "unknown")
        and wanted == "non_video"
    )


def snapshot(store, profile, now):
    # A short read transaction freezes contributions AND recommendation cooldown consistently.
    store.db.execute("BEGIN")
    try:
        rows = store.db.execute(
            "SELECT * FROM pool_items WHERE namespace=? ORDER BY rowid", (namespace(profile),)
        ).fetchall()
        blocked = store.blocked(profile.context, now, profile)
        latest = store.db.execute(
            "SELECT collected_at,run_id FROM pool_collections WHERE namespace=?",
            (namespace(profile),),
        ).fetchone()
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    refs = {(r["kind"], r["key"]): r for r in subscriptions(profile)}
    groups = defaultdict(list)
    hit_ids = defaultdict(set)
    counts = dict(
        expired_filtered=0, source_filtered=0, type_filtered=0, manual_filtered=0, rule_filtered=0
    )
    enabled = {
        r.id
        for r in profile.filters.rules
        if r.enabled and r.id in RULES and r.version == RULES[r.id].version
    }
    raw = []
    for row in rows:
        ref = refs.get((row["kind"], row["source_key"]))
        if not ref:
            counts["source_filtered"] += 1
            continue
        a = Article.model_validate(json.loads(row["data"])).model_dump()
        if not alive(a, row["first_seen"], profile, now):
            counts["expired_filtered"] += 1
            continue
        if not accepted_type(ref, a, profile):
            counts["type_filtered"] += 1
            continue
        a["filter_hits"] = marks(a)
        raw.append(a)
        for hit in a["filter_hits"]:
            hit_ids[hit["id"]].add(a["id"])
        if block_reason(a, profile.filters):
            counts["manual_filtered"] += 1
            continue
        if any(h["id"] in enabled for h in a["filter_hits"]):
            counts["rule_filtered"] += 1
            continue
        groups[(ref["kind"], ref["key"])].append(a)
    prepared = [
        ({k: ref[k] for k in ("kind", "key", "label")}, groups[key])
        for key, ref in refs.items()
        if groups[key]
    ]
    # Count before the model-input budget, with the same history/duration checks.
    unlimited = profile.model_copy(update={"max_candidates": max(1, len(rows))})
    eligible, _, _ = prepare(prepared, unlimited, blocked)
    selected, merged, base = prepare(prepared, profile, blocked)
    counts.update(base)
    result = dict(
        articles=selected,
        raw=raw,
        metrics=dict(counts, candidate_count=len(selected), collected_count=len(raw)),
        eligible_count=len(eligible),
        filter_counts={rule: len(hit_ids[rule]) for rule in RULES},
        last_collected_at=latest["collected_at"] if latest else None,
        collection_run_id=latest["run_id"] if latest else None,
    )
    return result


def migrate_pool(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='pool_items'").fetchone():
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS pool_items(namespace TEXT,kind TEXT,source_key TEXT,entry_key TEXT,
            video TEXT,first_seen TEXT,last_seen TEXT,data TEXT,PRIMARY KEY(namespace,kind,source_key,entry_key))""")
        db.execute("CREATE INDEX IF NOT EXISTS pool_video ON pool_items(namespace,video)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS pool_imports(run TEXT,id TEXT,status TEXT,reason TEXT,PRIMARY KEY(run,id))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS pool_collections(namespace TEXT PRIMARY KEY,collected_at TEXT,run_id TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS task_leases(kind TEXT PRIMARY KEY,owner TEXT,pid INTEGER,expires REAL,job TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS task_jobs(id TEXT PRIMARY KEY,kind TEXT,created TEXT,data TEXT)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS collection_schedule(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER,next_due REAL,interval INTEGER)"
        )
        from .schemas import Profile

        # Only source-evidenced old candidates are imported. History/feedback JSON stays untouched.
        for row in db.execute(
            "SELECT c.run,c.id,c.data,u.data AS run_data,u.created FROM candidates c JOIN runs u ON u.id=c.run ORDER BY u.created,c.rowid"
        ).fetchall():
            try:
                p = Profile.model_validate(json.loads(row["run_data"])["profile"])
                a = Article.model_validate(json.loads(row["data"])).model_dump()
                now = parse_time(a.get("fetched_at")) or parse_time(row["created"])
                refs = a["sources"] or [
                    dict(kind="keyword_search", key=k, label="关键词 · " + k)
                    for k in a["matched_keywords"]
                ]
                if now and refs:
                    for ref in refs:
                        upsert(db, p, ref, a, now)
                    status, reason = "imported", "source_evidence_and_timestamp"
                else:
                    status, reason = "skipped", "missing_source_evidence_or_timestamp"
            except (ValueError, KeyError, TypeError):
                status, reason = "skipped", "incompatible_candidate_or_profile"
            db.execute(
                "INSERT OR REPLACE INTO pool_imports VALUES(?,?,?,?)",
                (row["run"], row["id"], status, reason),
            )
        db.execute("PRAGMA user_version=3")
        db.commit()
    except Exception:
        db.rollback()
        raise
