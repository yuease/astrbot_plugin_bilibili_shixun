"""Separate a finished operation from verified data coverage."""

from ..core.chat_output import localize

STOP_WARNINGS = {
    "single_batch": "此来源只读取一页，尚未确认后续内容是否取完。",
    "batch_limit": "已达到本次页数上限，后续内容尚未采集。",
    "global_request_budget": "已达到本次请求数量上限，采集提前停止。",
    "global_time_budget": "已达到本次时间上限，采集提前停止。",
    "time_boundary": "已到采集时间边界，没有继续读取更早内容。",
    "missing_cursor": "接口仍有后续内容，但未返回可用分页位置。",
    "cursor_not_advanced": "接口分页位置没有推进，已停止重复读取。",
    "no_new_items": "后续页未出现新条目，已停止重复读取；不能确认完整性。",
    "request_failed": "此来源请求失败，请结合来源状态检查原因。",
}


def collection_summary(result):
    sources, warnings = [], []
    incomplete = False
    for value in result.get("source_results", [])[:40]:
        source = {
            k: value[k]
            for k in (
                "kind",
                "key",
                "label",
                "status",
                "count",
                "batches",
                "stop_reason",
                "has_more",
                "observed_oldest_at",
                "observed_newest_at",
            )
            if k in value
        }
        sources.append(source)
        stop = value.get("stop_reason")
        failed = value.get("status") not in ("ok", "empty")
        if stop in STOP_WARNINGS or failed:
            warnings.append(
                {
                    "code": stop if stop in STOP_WARNINGS else "source_failure",
                    "message": STOP_WARNINGS.get(stop, "此来源未成功完成采集。"),
                    "source": {k: value[k] for k in ("kind", "key", "label") if k in value},
                }
            )
        if (
            failed
            or stop
            in {
                "batch_limit",
                "global_request_budget",
                "global_time_budget",
                "time_boundary",
                "request_failed",
            }
            or (value.get("has_more") and stop not in {"end_of_feed", "empty_batch"})
        ):
            incomplete = True
    return {
        "data_completeness": "incomplete" if incomplete else "unknown",
        "data_complete": False if incomplete else None,
        "coverage_note": "仅包含本轮采集及已保留内容，任务结束不代表所有更新均已取完。",
        "source_item_count": sum(s.get("count", 0) for s in sources),
        "batches": sum(s.get("batches", 0) for s in sources),
        "retained_candidate_count": result.get("metrics", {}).get("collected_count"),
        "sources": sources,
        "warnings": warnings,
    }


def refresh_view(job):
    summary = job.get("collection_summary") or {
        "data_completeness": "unknown",
        "data_complete": None,
        "coverage_note": "此任务尚无扫描汇总，不能确认数据完整性。",
        "warnings": [],
    }
    result = {
        k: job[k]
        for k in (
            "id",
            "operation",
            "status",
            "started",
            "finished",
            "run_id",
            "error_code",
            "error_message",
            "reused_running",
        )
        if k in job
    }
    result.update(summary)
    result["warnings"] = list(summary.get("warnings", []))
    if job.get("warning"):
        result["warnings"].append({"code": "task_warning", "message": job["warning"]})
    finished = job["status"] in {"completed", "failed"}
    result.update(operation_finished=finished, operation_succeeded=job["status"] == "completed")
    if job["status"] == "failed":
        result.update(data_completeness="incomplete", data_complete=False)
        result["warnings"].append(
            {
                "code": job.get("error_code", "task_failed"),
                "message": job.get("error_message", "本轮任务失败，不能将已有数据视作刷新成功。"),
            }
        )
    if job["status"] == "running":
        result["guidance"] = (
            "采集仍在后台运行；可用action=status并设置wait_seconds继续有限等待，不要调用shell或sleep等待。尚不能声称采集结束。"
        )
        result["poll_after_seconds"] = 2
    elif job["status"] == "completed":
        result["guidance"] = (
            "本轮采集任务已结束，可以查询保留内容。请同时说明data_completeness和warnings中的实际限制，不要称全部更新已取完。"
        )
    else:
        result["guidance"] = "本轮采集失败，请说明错误原因；已有保留内容不代表本轮刷新成功。"
    result["timezone"] = "Asia/Shanghai"
    return localize(result)
