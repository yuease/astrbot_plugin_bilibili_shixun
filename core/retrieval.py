"""Deterministic lexical retrieval over scoped retained contributions, before any result budget."""

import copy
import hashlib
import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .agent_models import Coverage, Item, SourceEvidence, SourceStatus
from .core_errors import CoreError
from .errors import MESSAGES
from .filtering import allows
from .pool import accepted_type, namespace, parse_time
from .schemas import Article
from .sources import merge_article, subscriptions
from .text_evidence import clean_terms, documents, hits, normalize, select_spans


def hash_data(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def terms_for(question, explicit, aliases):
    # Retain the helper signature for callers; question is for model understanding only.
    try:
        terms = clean_terms(explicit)
    except (TypeError, ValueError):
        raise CoreError("INVALID_ARGUMENT") from None
    configured = {normalize(k): [normalize(x) for x in v] for k, v in aliases.items()}
    groups = [[t] + configured.get(t, []) for t in terms]
    return terms, groups


def resolve_range(spec, question, now):
    try:
        zone = ZoneInfo(spec.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise CoreError("INVALID_ARGUMENT") from None
    today = spec.period == "today"
    if today:
        day = now.astimezone(zone).date()
        start = datetime.combine(day, time.min, zone)
        end = datetime.combine(day + timedelta(days=1), time.min, zone)
    elif spec.period == "last_3_days":
        # Subtract in UTC so this remains exactly 72 hours across DST changes.
        from datetime import timezone

        end = now.astimezone(zone)
        start = (now.astimezone(timezone.utc) - timedelta(hours=72)).astimezone(zone)
    else:

        def bound(value):
            if not value:
                return None
            try:
                d = datetime.fromisoformat(value)
                return d.replace(tzinfo=zone) if d.tzinfo is None else d.astimezone(zone)
            except (ValueError, TypeError):
                raise CoreError("INVALID_ARGUMENT") from None

        start, end = bound(spec.start), bound(spec.end)
    if start and end and start >= end:
        raise CoreError("INVALID_ARGUMENT")
    return dict(
        start=start.isoformat() if start else None,
        end=end.isoformat() if end else None,
        timezone=spec.timezone,
    )


def matches_source(ref, selectors):
    return not selectors or any(
        s.kind == ref["kind"] and (s.key is None or normalize(s.key) == normalize(ref["key"]))
        for s in selectors
    )


def access_stamp(profile):
    return hash_data(
        [
            namespace(profile),
            profile.source_plan.model_dump(),
            profile.keywords,
            profile.filters.model_dump(),
            profile.retrieval.model_dump(),
        ]
    )


def searchable_fields(article):
    body = (
        article.get("body_text")
        if article.get("body_text") is not None
        else article.get("excerpt", "")
    )
    contexts = [
        c.get("text", "")
        for c in article.get("dynamic_contexts", [])
        if isinstance(c.get("text"), str)
    ]
    return [
        article["title"],
        body,
        " ".join(article.get("tags", [])),
        article.get("author", ""),
        *contexts,
    ]


def canonical_read(db, article):
    mapped = {
        r[0]
        for alias in article.get("source_ids", [])
        for r in db.execute("SELECT video FROM aliases WHERE alias=?", (alias,))
    }
    if len(mapped) == 1:
        article["id"] = next(iter(mapped))
    return article


def retrieve(
    store, profile, request, absolute, groups, check_deadline=lambda: None, *, collection_at=None
):
    # No personal TTL, cooldown, max_candidates, exploration or learning ranking is consulted here.
    store.db.execute("BEGIN")
    try:
        rows = store.db.execute(
            "SELECT * FROM pool_items WHERE namespace=?"
            + (" AND last_seen=?" if collection_at is not None else "")
            + " ORDER BY rowid",
            (namespace(profile), collection_at)
            if collection_at is not None
            else (namespace(profile),),
        ).fetchall()
        temp = (
            []
            if collection_at is not None
            else store.db.execute(
                "SELECT * FROM temporary_items WHERE namespace=? ORDER BY rowid",
                (namespace(profile),),
            ).fetchall()
        )
        aliases = {r["alias"]: r["video"] for r in store.db.execute("SELECT * FROM aliases")}
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    refs = {
        (r["kind"], r["key"]): {k: r[k] for k in ("kind", "key", "label")}
        for r in subscriptions(profile)
    }
    retained = []
    for row in rows:
        ref = refs.get((row["kind"], row["source_key"]))
        if ref:
            retained.append((ref, row, False))
    for row in temp:
        retained.append(
            (
                dict(
                    kind="temporary_keyword",
                    key=row["keyword"],
                    label="临时视频搜索 · " + row["keyword"],
                ),
                row,
                True,
            )
        )
    found = {}
    missing = 0
    start, end = parse_time(absolute["start"]), parse_time(absolute["end"])
    for ref, row, temporary in retained:
        check_deadline()
        if not matches_source(ref, request.sources):
            continue
        if request.temporary_query_id and (
            not temporary or row["query_id"] != request.temporary_query_id
        ):
            continue
        a = Article.model_validate(json.loads(row["data"])).model_dump()
        if request.has_images is True and not a["images"]:
            continue
        if request.has_images is False and (a["images"] or a.get("images_status") != "known"):
            continue
        if not temporary and not accepted_type(ref, a, profile):
            continue
        if request.content_types and a["content_type"] not in request.content_types:
            continue
        if (
            request.max_duration_seconds is not None
            and a["content_type"] == "video"
            and (
                a["duration_seconds"] is None
                or a["duration_seconds"] > request.max_duration_seconds
            )
        ):
            continue
        if not allows(a, profile.filters):
            continue
        published = parse_time(a.get("published_at"))
        if not published:
            a["published_at"] = None
            missing += 1
            if start or end:
                continue
        elif start and published < start or end and published >= end:
            continue
        mapped = {aliases[x] for x in a["source_ids"] if x in aliases}
        if len(mapped) == 1:
            a["id"] = next(iter(mapped))
        a["sources"] = [ref]
        proof = SourceEvidence(
            **ref,
            collected_at=row["last_seen"],
            published_at=a.get("published_at"),
            dynamic_id=a.get("dynamic_id"),
            query_id=row["query_id"] if temporary else None,
            temporary=temporary,
            url=a["url"],
            author=a["author"],
            author_uid=a.get("author_uid"),
        ).model_dump()
        contribution = dict(article=a, proof=proof, entry_key=row["entry_key"])
        if a["id"] in found:
            # This is a display projection only; searchable text stays in its source record.
            merge_article(found[a["id"]]["article"], dict(a))
            found[a["id"]]["proof"].append(proof)
            found[a["id"]]["contributions"].append(contribution)
        else:
            found[a["id"]] = dict(
                article=copy.deepcopy(a), proof=[proof], contributions=[contribution]
            )
    result = []
    for identity, entry in found.items():
        a = entry["article"]
        entry["contributions"].sort(
            key=lambda c: (c["proof"]["kind"], c["proof"]["key"], c["entry_key"])
        )
        docs = [d for c in entry["contributions"] for d in documents(c["article"], c["proof"])]
        for d in docs:
            d["hits"] = hits(d["text"], groups)
        matched = {k for d in docs for s, e, k in d["hits"]}
        if groups and len(matched) != len(groups):
            continue
        score = sum(
            5 if any(k == g for d in docs if d["field"] == "title" for s, e, k in d["hits"]) else 1
            for g in range(len(groups))
        )
        matching_sources = [d["source"] for d in docs if d["hits"]] if groups else entry["proof"]
        times = [
            parse_time(p["published_at"]) for p in matching_sources if parse_time(p["published_at"])
        ]
        stamp = max(times).timestamp() if times else float("-inf")
        entry.update(
            score=score,
            sort_time=stamp,
            texts=[d["text"] for d in docs],
            documents=docs,
            groups=groups,
        )
        result.append(entry)
    result.sort(key=lambda e: (-e["score"], -e["sort_time"], e["article"]["id"]))
    # Ignore fetch timestamps in content versions; evidence is rehydrated on cache/replay.
    content_version = hash_data(
        [
            [
                dict(
                    article={
                        k: v
                        for k, v in c["article"].items()
                        if k not in ("fetched_at", "filter_hits")
                    },
                    source={
                        k: v for k, v in c["proof"].items() if k not in ("collected_at", "query_id")
                    },
                    entry_key=c["entry_key"],
                )
                for c in e["contributions"]
            ]
            for e in result
        ]
    )
    return result, missing, content_version


def fragment(entry, groups, limit):
    spans, truncated = select_spans(entry, groups, limit)
    return spans[0]["text"] if spans else "", truncated


def item_view(entry, groups, limit, *, context_chars=100, preferred_span=None):
    spans, truncated = (
        select_spans(entry, groups, limit) if preferred_span is None else ([preferred_span], True)
    )
    selected = spans[0] if spans else None
    source = selected["source"] if selected else entry["proof"][0]
    # Title, author, URL and time belong to the same contribution as the snippet/quote.
    a = next(c["article"] for c in entry["contributions"] if c["proof"] == source)
    snippet = selected["text"] if selected else ""
    matched = source["published_at"]
    selected_doc = next(
        (
            d
            for d in entry.get("documents", [])
            if selected and d["field"] == selected["field"] and d["source"] == source
        ),
        None,
    )
    snippet_cut = bool(
        selected
        and selected_doc
        and (selected["start"] > 0 or selected["end"] < len(selected_doc["text"]))
    )
    return Item(
        id=a["id"],
        title=a["title"],
        snippet=snippet,
        author=a["author"],
        author_uid=a.get("author_uid"),
        published_at=a.get("published_at"),
        matched_published_at=matched,
        time_basis="publication" if matched else "missing",
        activity_type=a.get("activity_type", "original"),
        activity_url=a.get("activity_url"),
        forwarded_by=a.get("forwarded_by"),
        original_published_at=a.get("original_published_at"),
        url=a["url"],
        content_type=a["content_type"],
        duration_seconds=a.get("duration_seconds"),
        sources=entry["proof"][:8],
        dynamic_contexts=[
            {
                k: (v[:context_chars] if k == "text" and isinstance(v, str) else v)
                for k, v in c.items()
                if k
                in (
                    "id",
                    "url",
                    "author",
                    "author_uid",
                    "original_author_uid",
                    "text",
                    "published_at",
                )
            }
            for c in a.get("dynamic_contexts", [])[:4]
        ],
        sources_truncated=len(entry["proof"]) > 8,
        contexts_truncated=len(a.get("dynamic_contexts", [])) > 4
        or any(len(c.get("text", "")) > context_chars for c in a.get("dynamic_contexts", [])),
        snippet_evidence=selected,
        snippet_truncated=snippet_cut,
        source_text_limited=a.get("body_text") is None or a.get("body_truncated", False),
    )


def source_statuses(store, profile, include_disabled=False):
    active_refs = {(r["kind"], r["key"]): r for r in subscriptions(profile)}
    refs = dict(active_refs)
    if include_disabled:
        plan = profile.source_plan.model_copy(deep=True)
        plan.keyword_search.enabled = bool(profile.keywords)
        for u in plan.ups:
            u.enabled = True
        plan.following.enabled = bool(plan.following.uid)
        refs.update(
            {
                (r["kind"], r["key"]): r
                for r in subscriptions(profile.model_copy(update={"source_plan": plan}))
            }
        )
    temporary_keys = {
        r[0]
        for r in store.db.execute(
            "SELECT DISTINCT keyword FROM temporary_items WHERE namespace=?", (namespace(profile),)
        )
    }
    temporary_keys.update(
        r[0]
        for r in store.db.execute(
            "SELECT source_key FROM source_observations WHERE namespace=? AND kind='temporary_keyword'",
            (namespace(profile),),
        )
    )
    for r in [(k,) for k in sorted(temporary_keys)]:
        refs[("temporary_keyword", r[0])] = dict(
            kind="temporary_keyword", key=r[0], label="临时视频搜索 · " + r[0]
        )
    result = []
    for key, ref in refs.items():
        temporary = ref["kind"] == "temporary_keyword"
        observation = store.db.execute(
            "SELECT * FROM source_observations WHERE namespace=? AND kind=? AND source_key=?",
            (namespace(profile), *key),
        ).fetchone()
        scan_row = store.db.execute(
            "SELECT data FROM source_scans WHERE namespace=? AND kind=? AND source_key=?",
            (namespace(profile), *key),
        ).fetchone()
        scan = json.loads(scan_row[0]) if scan_row else {}
        count = (
            store.db.execute(
                "SELECT count(*) FROM temporary_items WHERE namespace=? AND keyword=?",
                (namespace(profile), ref["key"]),
            ).fetchone()[0]
            if temporary
            else store.db.execute(
                "SELECT count(*) FROM pool_items WHERE namespace=? AND kind=? AND source_key=?",
                (namespace(profile), *key),
            ).fetchone()[0]
        )
        if not temporary and key not in active_refs:
            availability = "disabled"
        elif not observation:
            availability = "unknown"
        elif observation["status"] in ("ok", "empty"):
            availability = "ready"
        else:
            availability = "partial" if observation["last_success"] else "error"
        if availability != "disabled" and scan.get("status") not in (None, "ok", "empty"):
            availability = "partial" if observation and observation["last_success"] else "error"
        code = observation["error_code"] if observation else None
        result.append(
            SourceStatus(
                kind=ref["kind"],
                key=ref["key"],
                label=ref["label"],
                availability=availability,
                last_checked_at=observation["checked"] if observation else None,
                last_success_at=observation["last_success"] if observation else None,
                error_code=code,
                error_message=MESSAGES.get(code) if code else None,
                retained_count=count,
                temporary=temporary,
                scan=scan,
            )
        )
    return result


def coverage(store, profile, request, absolute, now, missing):
    statuses = [
        s
        for s in source_statuses(store, profile)
        if matches_source(s.model_dump(), request.sources)
    ]
    reasons = ["retained_sampling"]
    incomplete = False
    if not statuses:
        reasons.append("no_collected_sources_in_scope")
    for s in statuses:
        if s.availability in ("error", "partial"):
            reasons.append("source_failure")
            incomplete = True
        stop = s.scan.get("stop_reason")
        if stop in ("batch_limit", "global_request_budget", "global_time_budget", "time_boundary"):
            reasons.append(stop)
            incomplete = True
        elif stop not in ("end_of_feed", "empty_batch"):
            reasons.append(stop or "scan_extent_unknown")
            if s.scan.get("has_more"):
                incomplete = True
        if not s.last_success_at:
            reasons.append("source_never_succeeded")
        elif absolute["start"] and parse_time(s.last_success_at) < parse_time(absolute["start"]):
            reasons.append("source_not_collected_in_requested_period")
    if missing:
        reasons.append("missing_publication_time")
    return Coverage(
        **absolute,
        as_of=now.isoformat(),
        insufficient=True,
        completeness="incomplete" if incomplete else "unknown",
        reasons=list(dict.fromkeys(reasons)),
        missing_publication_count=missing,
        sources=statuses[:8],
        sources_truncated=len(statuses) > 8,
    )
