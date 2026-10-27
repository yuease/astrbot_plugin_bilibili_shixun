"""Automatic context follows settled collections, trusted identity and current policy."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.provider import ProviderRequest
from astrbot_plugin_bilibili_shixun.core.bilibili_items import video_batch
from astrbot_plugin_bilibili_shixun.core.source_types import SourceError
from astrbot_plugin_bilibili_shixun.services.collection_context import END, START
from astrbot_plugin_bilibili_shixun.services.errors import PluginError
from astrbot_plugin_bilibili_shixun.services.settings import Settings
from astrbot_plugin_bilibili_shixun.services.users import UserApplications
from test_smoke import LOGGER, seed, video
from test_users import event
from test_users import plugin as shared_plugin

plugin = shared_plugin


def payload(req):
    block = req.system_prompt.split(START + "\n", 1)[1].split("\n" + END, 1)[0]
    return json.loads(block.split("\n", 1)[1])


async def collected(plugin, items=None):
    app = await plugin.users.chat(event("10001"))
    await app.io(seed, app.runtime)
    app.login.state.update(status="valid", uid="1001")
    app.source.fetch = lambda *args, **kwargs: video_batch(items or [video()])
    job = await app.refresh_sources("context-first", wait_seconds=10)
    assert job["status"] == "completed"
    return app


@pytest.mark.asyncio
async def test_collected_context_is_bounded_idempotent_and_preserves_request(plugin):
    app = await collected(plugin)
    req = ProviderRequest(system_prompt="人格设定", prompt="聊聊最近采集的内容", contexts=[])
    await plugin.inject_collection_context(event("10001"), req)
    data = payload(req)
    assert data["available_count"] == data["shown_count"] == 1
    assert data["items"][0]["title"] == "Codex 原文"
    assert data["items"][0]["snippet"] == "作者介绍 Codex 的使用方法"
    assert data["items"][0]["url"].startswith("https://www.bilibili.com/")
    assert data["warnings"] and data["data_completeness"] == "unknown"
    assert req.system_prompt.startswith("人格设定") and len(req.system_prompt) < 6010
    assert req.contexts == [] and req.prompt == "聊聊最近采集的内容"
    before = req.system_prompt
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == before
    assert app.context.llm_generate.await_count == 0


@pytest.mark.asyncio
async def test_only_latest_batch_is_injected_and_current_source_switch_applies(plugin):
    app = await collected(plugin)
    app.source.fetch = lambda *args, **kwargs: video_batch([video(2)])
    done = await app.refresh_sources("context-second", wait_seconds=10)
    req = ProviderRequest()
    await plugin.inject_collection_context(event("10001"), req)
    assert payload(req)["run_id"] == done["run_id"]
    assert len(payload(req)["items"]) == 1
    assert payload(req)["items"][0]["url"].endswith("BV1Smoke0002")
    settings = await app.tool("get_settings", {})
    await app.change_settings(
        "set_subscription_enabled",
        {"source": "keyword_search", "enabled": False, "expected_version": settings["version"]},
        "disable-context-source",
    )
    await plugin.inject_collection_context(event("10001"), req)
    assert payload(req)["items"] == []


@pytest.mark.asyncio
async def test_other_users_groups_and_unknown_chats_cannot_receive_context(plugin):
    await collected(plugin)
    await plugin.users.chat(event("20002"))
    count = len(plugin.users.apps)
    group = event("10001")
    group.is_private_chat = lambda: False
    for who in (event("20002"), event("30003"), event("10001", platform="other"), group):
        req = ProviderRequest(system_prompt="原设定")
        await plugin.inject_collection_context(who, req)
        assert req.system_prompt == "原设定"
    assert len(plugin.users.apps) == count


@pytest.mark.asyncio
async def test_failed_refresh_and_account_change_do_not_reuse_success_context(plugin):
    app = await collected(plugin)
    req = ProviderRequest(system_prompt="原设定")
    await plugin.inject_collection_context(event("10001"), req)
    assert START in req.system_prompt

    def fail(*args, **kwargs):
        raise SourceError("SOURCE_RATE_LIMIT")

    app.source.fetch = fail
    assert (await app.refresh_sources("context-failed", wait_seconds=10))["status"] == "failed"
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == "原设定"
    await app.io(app.runtime.bind_account, "2002")
    assert await app.collection_context() == ""


@pytest.mark.asyncio
async def test_limits_disable_and_running_collection(plugin):
    items = [dict(video(i), description="长正文" * 300) for i in range(1, 21)]
    app = await collected(plugin, items)
    req = ProviderRequest()
    await plugin.inject_collection_context(event("10001"), req)
    assert 0 < payload(req)["shown_count"] <= 8 and payload(req)["truncated"]
    app.settings = replace(app.settings, collection_context_max_chars=1000)
    app.runtime.settings = app.settings
    await plugin.inject_collection_context(event("10001"), req)
    assert len(req.system_prompt) <= 1000
    assert payload(req)["truncated"]
    app.active = "in-flight"
    try:
        await plugin.inject_collection_context(event("10001"), req)
        assert req.system_prompt == ""
    finally:
        app.active = None
    plugin.settings = replace(plugin.settings, collection_context_enabled=False)
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == ""


@pytest.mark.asyncio
async def test_restored_collection_is_available_without_a_new_collection(plugin):
    app = await collected(plugin)
    # Match a normal logged-in reload, without contacting Bilibili.
    app.login.state.update(credential={"sessdata": "synthetic"})
    await app.login.persist()
    await plugin.users.close()
    plugin.users = UserApplications(plugin.users.root, plugin.context, plugin.settings, LOGGER)
    await plugin.users.start()
    req = ProviderRequest()
    await plugin.inject_collection_context(event("10001"), req)
    assert payload(req)["shown_count"] == 1
    assert plugin.context.llm_generate.await_count == 0


@pytest.mark.asyncio
async def test_context_error_does_not_break_chat(plugin, monkeypatch):
    app = await collected(plugin)
    monkeypatch.setattr(app, "collection_context", AsyncMock(side_effect=RuntimeError("offline")))
    req = ProviderRequest(system_prompt="原设定")
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == "原设定"


@pytest.mark.parametrize(
    "config",
    [
        {"collection_context_enabled": "true"},
        {"collection_context_max_chars": True},
        {"collection_context_max_chars": 999},
        {"collection_context_max_chars": 20001},
    ],
)
def test_invalid_context_config(config):
    with pytest.raises(PluginError):
        Settings.from_config(config)
