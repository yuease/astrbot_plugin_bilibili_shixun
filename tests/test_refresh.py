"""Bounded waiting, durable receipts and honest refresh summaries."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot_plugin_bilibili_shixun.core.bilibili_items import dynamic_batch, video_batch
from astrbot_plugin_bilibili_shixun.core.source_types import SourceError
from astrbot_plugin_bilibili_shixun.services.application import Application
from astrbot_plugin_bilibili_shixun.services.errors import PluginError
from astrbot_plugin_bilibili_shixun.services.settings import Settings
from test_smoke import LOGGER, card, seed, video


@pytest_asyncio.fixture
async def app(tmp_path):
    context = SimpleNamespace(
        get_config=lambda *args: {"admins_id": ["10001"]}, llm_generate=AsyncMock()
    )
    value = Application(
        tmp_path,
        context,
        Settings(),
        LOGGER,
        identity={
            "kind": "chat",
            "adapter": "aiocqhttp",
            "platform_id": "test-bot",
            "sender": "10001",
        },
    )
    await value.start()
    await value.io(seed, value.runtime)
    value.login.state.update(status="valid", uid="1001")
    try:
        yield value
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_wait_returns_finished_receipt_and_replays_without_recollecting(app):
    calls = []

    def fetch(*args, **kwargs):
        calls.append(1)
        return video_batch([video()])

    app.source = SimpleNamespace(fetch=fetch)
    done = await app.refresh_sources("wait-once", wait_seconds=10)
    assert done["status"] == "completed" and done["operation_finished"]
    assert done["operation_succeeded"] and not done["wait_timed_out"]
    assert "refresh_complete" not in done and done["data_complete"] is None
    assert done["data_completeness"] == "unknown" and done["source_item_count"] == 1
    assert done["warnings"][0]["code"] == "single_batch"
    replay = await app.refresh_sources("wait-once", wait_seconds=10)
    assert replay["id"] == done["id"] and len(calls) == 1
    assert app.context.llm_generate.await_count == 0


@pytest.mark.asyncio
async def test_wait_timeout_does_not_cancel_the_collection(app):
    gate, entered = threading.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()

    def fetch(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert gate.wait(5)
        return video_batch([video()])

    app.source = SimpleNamespace(fetch=fetch)
    try:
        first = await app.refresh_sources("slow", wait_seconds=0)
        await asyncio.wait_for(entered.wait(), 2)
        waiting = await app.refresh_sources("status", status_only=True, wait_seconds=1)
        assert waiting["status"] == "running" and waiting["wait_timed_out"]
        assert not waiting["operation_finished"] and not app.job_tasks[first["id"]].cancelled()
        gate.set()
        done = await app.refresh_sources("status", status_only=True, wait_seconds=2)
        assert done["status"] == "completed" and done["id"] == first["id"]
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_concurrent_waiters_share_the_same_collection(app):
    gate = threading.Event()
    calls = []

    def fetch(*args, **kwargs):
        calls.append(1)
        assert gate.wait(5)
        return video_batch([video()])

    app.source = SimpleNamespace(fetch=fetch)
    asyncio.get_running_loop().call_later(0.1, gate.set)
    try:
        first, second = await asyncio.gather(
            app.refresh_sources("waiter-one", wait_seconds=2),
            app.refresh_sources("waiter-two", wait_seconds=2),
        )
        assert (
            first["id"] == second["id"]
            and first["operation_finished"]
            and second["operation_finished"]
        )
        assert len(calls) == 1
    finally:
        gate.set()


@pytest.mark.asyncio
async def test_page_limit_becomes_task_warning_without_claiming_complete_data(app):
    state = await app.io(app.runtime.state)
    plan, collection = state["profile"]["source_plan"], state["profile"]["collection"]
    plan["keyword_search"]["enabled"] = False
    plan["following"].update(enabled=True, uid="1001", scope="dynamics")
    collection["dynamic_batches"] = 1
    await app.io(
        app.runtime.save_profile,
        {
            "expected_version": state["profile_version"],
            "changes": {"source_plan": plan, "collection": collection},
            "request_id": "limit",
        },
    )
    app.source = SimpleNamespace(
        fetch=lambda *args, **kwargs: dynamic_batch(
            {"items": [card()], "has_more": True, "offset": "next"}
        )
    )
    done = await app.refresh_sources("limited", wait_seconds=5)
    assert done["operation_finished"] and done["data_complete"] is False
    assert done["data_completeness"] == "incomplete"
    assert any(warning["code"] == "batch_limit" for warning in done["warnings"])
    assert done["sources"][0]["has_more"] and done["batches"] == 1
    assert done["sources"][0]["observed_oldest_at"].endswith("+08:00")
    status = await app.refresh_sources("status", status_only=True)
    assert status["warnings"] == done["warnings"]


@pytest.mark.asyncio
async def test_failed_refresh_is_finished_but_not_successful_or_complete(app):
    def fetch(*args, **kwargs):
        raise SourceError("SOURCE_RATE_LIMIT")

    app.source = SimpleNamespace(fetch=fetch)
    done = await app.refresh_sources("failed", wait_seconds=5)
    assert done["status"] == "failed" and done["operation_finished"]
    assert not done["operation_succeeded"] and done["data_complete"] is False
    assert done["error_code"] and done["warnings"]


@pytest.mark.asyncio
async def test_legacy_finished_receipt_reports_unknown_coverage(app):
    job, _ = await app.io(app.jobs.start, "collect", "legacy", "1001")
    job.update(status="completed", warning="旧任务警告")
    await app.io(app.jobs.finish, job)
    result = await app.refresh_sources("status", status_only=True)
    assert result["operation_finished"] and result["data_complete"] is None
    assert result["warnings"] == [{"code": "task_warning", "message": "旧任务警告"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [-1, 21, True])
async def test_wait_limit_rejects_invalid_values(app, seconds):
    with pytest.raises(PluginError):
        await app.refresh_sources("invalid", wait_seconds=seconds)
