"""Resolve every requested selector without widening the caller's scope."""

from .pool import parse_time
from .retrieval import matches_source

MESSAGES = {
    "not_configured": "指定来源不存在于当前设置中；先查看或修改设置。",
    "disabled": "来源已配置但未启用；未查询其停用内容。",
    "not_collected": "来源已配置，但尚未成功采集；需要先刷新来源。",
    "collection_failed": "来源采集失败，当前没有可用保留内容。",
    "failed_with_retained": "最近采集失败，返回的只能是已有旧内容。",
    "snapshot_older_than_range": "最近成功采集时间早于查询窗口，不能据此断言该时段没有更新。",
    "temporary_search_required": "没有该关键词的可用临时结果；请先调用search_bilibili联网搜索，选择来源本身不会联网。",
    "collected_empty": "来源上次成功采集为空；不代表远端没有相关内容。",
    "ready": "仅在当前保留内容中匹配条件。",
}


def diagnose(statuses, selectors, absolute):
    requested = selectors or [None]
    result = []
    usable = {"ready", "collected_empty"}
    for selector in requested:
        matched = (
            [s for s in statuses if s.availability != "disabled"]
            if selector is None
            else [s for s in statuses if matches_source(s.model_dump(), [selector])]
        )
        if not matched:
            state = (
                "temporary_search_required"
                if selector is not None and selector.kind == "temporary_keyword"
                else "not_configured"
            )
            result.append(
                {
                    "selector": selector.model_dump(exclude_none=True)
                    if selector
                    else {"scope": "enabled_and_retained"},
                    "state": state,
                    "message": MESSAGES[state],
                    "matched_sources": [],
                }
            )
            continue
        sources = []
        for source in matched:
            if source.availability == "disabled":
                state = "disabled"
            elif source.availability in {"error", "partial"}:
                state = "failed_with_retained" if source.retained_count else "collection_failed"
            elif not source.last_success_at:
                state = "not_collected"
            elif absolute.get("start") and parse_time(source.last_success_at) < parse_time(
                absolute["start"]
            ):
                state = "snapshot_older_than_range"
            else:
                state = "ready" if source.retained_count else "collected_empty"
            sources.append(
                {
                    "kind": source.kind,
                    "key": source.key,
                    "state": state,
                    "retained_count": source.retained_count,
                }
            )
        states = {s["state"] for s in sources}
        state = (
            next(iter(states))
            if len(states) == 1
            else "partial"
            if states & usable
            else "unavailable"
        )
        result.append(
            {
                "selector": selector.model_dump(exclude_none=True)
                if selector
                else {"scope": "enabled_and_retained"},
                "state": state,
                "message": MESSAGES.get(state, "部分来源状态不同，请逐项说明查询范围。"),
                "matched_sources": sources,
            }
        )
    states = [s["state"] for row in result for s in row["matched_sources"]] + [
        row["state"] for row in result if not row["matched_sources"]
    ]
    scope_status = (
        "ready"
        if all(s in usable for s in states)
        else "partial"
        if any(s in usable or s == "failed_with_retained" for s in states)
        else "unavailable"
    )
    return result, scope_status, states


def query_outcome(entries, states):
    if entries:
        return "found"
    if any(s in {"collection_failed", "failed_with_retained"} for s in states):
        return "collection_failed"
    if any(s in {"ready", "collected_empty"} for s in states):
        return (
            "partial_scope_no_match"
            if any(s not in {"ready", "collected_empty"} for s in states)
            else "no_match"
        )
    unique = set(states)
    if unique == {"not_configured"}:
        return "source_not_configured"
    if unique == {"disabled"}:
        return "source_disabled"
    if unique == {"not_collected"}:
        return "not_collected"
    if unique == {"snapshot_older_than_range"}:
        return "stale_data"
    return "source_unavailable"
