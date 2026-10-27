"""Compact chat projections; evidence stays in storage, timestamps use one requested zone."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .attachments import attachment_summary
from .core_errors import CoreError

TIME_FIELDS = {
    "published",
    "published_at",
    "original_published_at",
    "collected_at",
    "last_checked_at",
    "last_success_at",
    "observed_oldest_at",
    "observed_newest_at",
    "scan_started_at",
    "scan_finished_at",
    "start",
    "end",
    "as_of",
    "started",
    "finished",
}
TYPE_NAMES = {"text": "text_dynamic", "image": "image_dynamic"}


def localize(value, timezone="Asia/Shanghai"):
    try:
        zone = ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise CoreError("INVALID_ARGUMENT") from None

    def visit(node, field=None):
        if isinstance(node, dict):
            return {k: visit(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [visit(v) for v in node]
        if field in TIME_FIELDS and isinstance(node, str):
            try:
                stamp = datetime.fromisoformat(node)
                return stamp.astimezone(zone).isoformat() if stamp.tzinfo else None
            except ValueError:
                return node if field == "published" else None
        return node

    return visit(value)


def attribution(entry, span):
    source = span["source"]
    contribution = next(c for c in entry["contributions"] if c["proof"] == source)
    article = contribution["article"]
    match = re.fullmatch(r"dynamic_contexts\[(\d+)\]\.text", span["field"])
    context = article.get("dynamic_contexts", [])[int(match[1])] if match else None
    repost = article.get("activity_type") == "repost"
    role = (
        "repost_comment"
        if context and repost and context.get("id") == article.get("dynamic_id")
        else "original"
    )
    return {
        k: v
        for k, v in {
            "author": context.get("author") if context else article["author"],
            "url": context.get("url") if context else article["url"],
            "published_at": context.get("published_at")
            if context
            else article.get("original_published_at")
            if repost
            else article.get("published_at"),
            "role": role,
        }.items()
        if v is not None
    }


def compact_item(item, entry, *, detail=False):
    value = item.model_dump() if hasattr(item, "model_dump") else item
    keys = ("id", "title", "author", "published_at", "url", "duration_seconds")
    result = {k: value[k] for k in keys if value.get(k) is not None}
    result["content_type"] = TYPE_NAMES.get(value["content_type"], value["content_type"])
    result["sources"] = [
        {k: source[k] for k in ("kind", "key", "label")} for source in value["sources"]
    ]
    if value.get("sources_truncated"):
        result["sources_truncated"] = True
    result["activity_type"] = value.get("activity_type", "original")
    result.update(attachment_summary(entry["article"]))
    if result["activity_type"] == "repost":
        result.update(
            time_basis="repost",
            activity_url=value.get("activity_url"),
            forwarded_by=value.get("forwarded_by"),
            original_published_at=value.get("original_published_at"),
        )
    if not detail:
        result.update(snippet=value["snippet"], snippet_truncated=value["snippet_truncated"])
        if value.get("source_text_limited"):
            result["source_text_limited"] = True
        span = value.get("snippet_evidence")
        if span:
            proof = attribution(entry, span)
            if proof["author"] != result["author"] or proof["role"] == "repost_comment":
                result["snippet_attribution"] = proof
        if result.get("snippet") == result["title"] and "snippet_attribution" not in result:
            result.pop("snippet")
    return {k: v for k, v in result.items() if v is not None or k == "has_attachments"}


def compact_coverage(scope):
    value = scope.model_dump()
    value.pop("insufficient", None)
    value["missing_publication_time_count"] = value.pop("missing_publication_count")
    value["sources"] = [
        s.model_dump(exclude_none=True, exclude={"temporary"}) for s in scope.sources
    ]
    return value
