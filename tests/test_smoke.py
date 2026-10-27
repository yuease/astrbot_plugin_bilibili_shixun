"""Small offline regression checks over real SDK types, storage and lifecycle."""

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from astrbot_plugin_bilibili_shixun.core.bilibili_items import dynamic_batch, video_batch
from astrbot_plugin_bilibili_shixun.core.library import RequestError
from astrbot_plugin_bilibili_shixun.services import bilibili_login as login_module
from astrbot_plugin_bilibili_shixun.services.errors import PluginError, log_failure
from astrbot_plugin_bilibili_shixun.services.jobs import JobStore
from astrbot_plugin_bilibili_shixun.services.runtime import Runtime
from astrbot_plugin_bilibili_shixun.services.settings import Settings

LOGGER = logging.getLogger("bilibili-shixun-smoke")
STAMP = int(datetime.now(timezone.utc).timestamp())


def offline_client(handler):
    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(handler), **kwargs)

    return Client


def video(index=1):
    return {
        "bvid": f"BV1Smoke{index:04d}",
        "aid": 100 + index,
        "title": "<em>Codex</em> 原文",
        "description": "作者介绍 Codex 的使用方法",
        "author": "样本作者",
        "mid": 2002,
        "pubdate": STAMP,
        "duration": "00:33",
        "pic": "https://i0.hdslb.com/bfs/archive/smoke.jpg",
    }


def card(index=1):
    return {
        "id_str": str(9000 + index),
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": {"mid": 2002, "name": "样本作者", "pub_ts": STAMP},
            "module_dynamic": {
                "desc": {"text": "Codex 更新说明"},
                "major": {
                    "archive": {
                        "bvid": video(index)["bvid"],
                        "aid": 100 + index,
                        "title": "Codex 原文",
                        "desc": "作者介绍 Codex 的使用方法",
                        "duration_text": "00:33",
                    }
                },
            },
        },
    }


def seed(runtime):
    runtime.initialize()
    runtime.bind_account("1001")
    state = runtime.state()
    plan = state["profile"]["source_plan"]
    plan["keyword_search"]["enabled"] = True
    body = {
        "expected_version": state["profile_version"],
        "changes": {"keywords": ["codex"], "source_plan": plan},
        "request_id": "initial-save",
    }
    runtime.save_profile(body)
    return body


class Model:
    model_name = "offline-model"
    usage = None
    calls = 0

    def select(self, prompt, schema):
        self.calls += 1
        data = json.loads(prompt.split("\n", 1)[1])
        return schema.model_validate(
            {
                "picks": [
                    {
                        "id": data["articles"][0]["id"],
                        "summary": "离线样本摘要",
                        "reason": "匹配兴趣",
                        "action": "打开原文",
                        "base_score": 80,
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    "value",
    [
        {"model_timeout_seconds": 1},
        {"bili_check_interval_minutes": 0},
        {"bili_auto_relogin": "true"},
    ],
)
def test_config_rejects_invalid_ranges(value):
    with pytest.raises(PluginError):
        Settings.from_config(value)
    assert not hasattr(Settings(), "rsshub_url")


def test_video_and_forwarded_dynamic_keep_attribution_and_cursor():
    result = video_batch([video()])
    assert result[0].title == "Codex 原文" and result[0].duration_seconds == 33
    original = card()
    forwarded = {
        "id_str": "9100",
        "type": "DYNAMIC_TYPE_FORWARD",
        "orig": original,
        "modules": {
            "module_author": {"mid": 3003, "name": "转发者", "pub_ts": STAMP},
            "module_dynamic": {"desc": {"text": "转发者的独立观点"}},
        },
    }
    batch = dynamic_batch({"items": [forwarded], "has_more": True, "offset": "opaque_next"})
    assert batch.cursor == "opaque_next" and batch.has_more
    assert batch[0].author == "样本作者" and batch[0].original_author_uid == "2002"
    assert batch[0].dynamic_contexts[0]["author"] == "转发者"
    assert batch[0].dynamic_contexts[1]["author"] == "样本作者"


def test_collect_search_briefing_feedback_and_account_scope(tmp_path):
    runtime = Runtime(tmp_path, Settings())
    initial = seed(runtime)
    assert runtime.save_profile(initial)["replayed"]
    source = SimpleNamespace(fetch=lambda *args, **kwargs: video_batch([video()]))
    model = Model()
    collected = runtime.run_job("collect", "collect-smoke", source, model)
    assert collected["metrics"]["logical_model_calls"] == 0 and model.calls == 0
    found = runtime.tool("query_updates", {"question": "Codex 有什么更新", "terms": ["codex"]})
    assert found["outcome"] == "found" and found["retrieval_only"] and model.calls == 0
    item = runtime.tool("get_item", {"item_id": found["items"][0]["id"]})
    assert "使用方法" in item["body"]
    generated = runtime.run_job("generate", "briefing-smoke", source, model)
    assert generated["status"] == "completed" and model.calls == 1
    detail = runtime.detail("briefing-smoke")
    feedback = runtime.feedback(
        {
            "recommendation": detail["picks"][0]["recommendation_id"],
            "value": "useful",
            "key": "feedback-once",
        }
    )
    assert runtime.state()["stats"]["feedback_count"] == 1
    runtime.feedback({"event": feedback["event"], "key": "undo-once"}, True)
    assert runtime.state()["stats"]["feedback_count"] == 0
    assert runtime.detail("briefing-smoke", True)["report"] == generated["report"]
    runtime.bind_account("1002")
    assert runtime.tool("search_items", {"terms": ["codex"]})["items"] == []
    assert runtime.state()["runs"] == []
    with pytest.raises(RequestError):
        runtime.detail("briefing-smoke")


def test_large_settings_versioned_save_and_replay(tmp_path):
    runtime = Runtime(tmp_path, Settings())
    seed(runtime)
    body = {
        "expected_version": runtime.state()["profile_version"],
        "changes": {"interests": "测" * 6000},
        "interest_mode": "same_goal",
        "request_id": "long-interest",
    }
    runtime.save_profile(body)
    runtime.save_profile(body)
    state = runtime.state()
    assert state["profile"]["interests"] == "测" * 6000
    second = dict(body, expected_version=state["profile_version"], request_id="long-interest-2")
    runtime.save_profile(second)
    stale = dict(body, request_id="stale-version")
    with pytest.raises(RequestError) as error:
        runtime.save_profile(stale)
    assert error.value.status == 409


def test_task_receipt_replay_and_scope(tmp_path):
    store = JobStore(tmp_path)
    store.initialize()
    first, created = store.start("generate", "same-id", "1001")
    assert created and store.start("generate", "same-id", "1001")[1] is False
    with pytest.raises(PluginError):
        store.start("generate", "same-id", "1002")
    assert store.latest("generate", "1002") is None
    first.update(status="completed")
    store.finish(first)
    assert store.latest("generate", "1001")["status"] == "completed"


def owner_event():
    return SimpleNamespace(
        unified_msg_origin="smoke-qq:FriendMessage:10001",
        get_sender_id=lambda: "10001",
        get_platform_id=lambda: "smoke-qq",
        get_platform_name=lambda: "aiocqhttp",
        send=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "qr_base",
    [
        "https://passport.bilibili.com/h5-app/passport/login/scan",
        "https://account.bilibili.com/h5/account-h5/auth/scan-web",
    ],
)
async def test_qr_login_set_cookie_persistence_and_logout(tmp_path, monkeypatch, qr_base):
    real_sleep = asyncio.sleep
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "url": qr_base + "?qrcode_key=smoke",
                        "qrcode_key": "smoke",
                    },
                },
            )
        if request.url.path.endswith("/poll"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"code": 0}},
                headers=[
                    ("Set-Cookie", "SESSDATA=synthetic-session; Domain=.bilibili.com; Path=/"),
                    ("Set-Cookie", "bili_jct=synthetic-csrf; Domain=.bilibili.com; Path=/"),
                ],
            )
        assert "SESSDATA=synthetic-session" in request.headers.get("cookie", "")
        return httpx.Response(
            200, json={"code": 0, "data": {"isLogin": True, "mid": 1001, "uname": "样本账号"}}
        )

    monkeypatch.setattr(login_module.httpx, "AsyncClient", offline_client(handler))

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(login_module.asyncio, "sleep", fast_sleep)
    installed = []

    async def install(uid):
        installed.append(uid)

    context = SimpleNamespace(
        get_config=lambda *args: {"admins_id": ["10001"]}, send_message=AsyncMock(return_value=True)
    )
    login = login_module.BilibiliLogin(tmp_path, context, Settings(), LOGGER, install)
    event = owner_event()
    try:
        await login.request_login(event)
        await asyncio.wait_for(login.poll_task, 5)
        assert login.uid == "1001" and installed == ["1001"]
        assert "synthetic-session" not in json.dumps(login.public_state())
        saved = json.loads(login.path.read_text(encoding="utf-8"))
        assert saved["credential"]["sessdata"] == "synthetic-session"
        image = event.send.call_args_list[0].args[0].chain[1]
        assert image.file.startswith("base64://")
        await login.logout(event)
        assert login.uid == "" and installed[-1] == ""
        assert json.loads(login.path.read_text(encoding="utf-8"))["credential"] == {}
        assert len(calls) == 3
    finally:
        await login.close()


@pytest.mark.parametrize(
    "payload, reason",
    [
        ([], "invalid_response"),
        ({"code": -412}, "api_rejected"),
        ({"code": 0, "data": None}, "invalid_data"),
        ({"code": 0, "data": {"url": None}}, "invalid_url"),
        (
            {
                "code": 0,
                "data": {
                    "url": "https://account.bilibili.com.evil.example/h5/account-h5/auth/scan-web",
                    "qrcode_key": "smoke",
                },
            },
            "unsupported_url",
        ),
        (
            {
                "code": 0,
                "data": {
                    "url": "http://account.bilibili.com/h5/account-h5/auth/scan-web",
                    "qrcode_key": "smoke",
                },
            },
            "unsupported_url",
        ),
        (
            {
                "code": 0,
                "data": {
                    "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web",
                    "qrcode_key": "bad key",
                },
            },
            "invalid_key",
        ),
    ],
)
def test_qr_rejects_invalid_responses_with_safe_reason(payload, reason):
    with pytest.raises(PluginError) as error:
        login_module.qr_payload(payload)
    assert error.value.reason == reason


def test_qr_diagnostics_log_codes_without_response_secrets(caplog):
    log_failure(
        LOGGER, "login", "qr", PluginError("BILI_QR_FAILED", reason="api_rejected", api_code=-412)
    )
    log_failure(LOGGER, "login", "qr", PluginError("synthetic-session", reason="synthetic-qr-key"))
    assert "code=BILI_QR_FAILED reason=api_rejected api_code=-412" in caplog.text
    assert "synthetic-session" not in caplog.text and "synthetic-qr-key" not in caplog.text


@pytest.mark.asyncio
async def test_health_http_failure_does_not_expire_cookie(tmp_path, monkeypatch):
    response = {"status": 503, "body": {}}
    monkeypatch.setattr(
        login_module.httpx,
        "AsyncClient",
        offline_client(lambda request: httpx.Response(response["status"], json=response["body"])),
    )
    login = login_module.BilibiliLogin(tmp_path, None, Settings(), LOGGER, AsyncMock())
    login.state.update(
        status="valid", uid="1001", revision="first", credential={"sessdata": "synthetic"}
    )
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await login.check()
        assert login.uid == "1001"
        response.update(status=200, body={"code": -101})
        await login.check()
        assert login.public_state()["status"] == "expired"
    finally:
        await login.close()


@pytest.mark.asyncio
async def test_auto_expiry_sends_one_qr_to_bound_private_chat(tmp_path, monkeypatch):
    real_sleep = asyncio.sleep

    def handler(request):
        if request.url.path.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "url": "https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=auto",
                        "qrcode_key": "auto",
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {"code": 86038}})

    monkeypatch.setattr(login_module.httpx, "AsyncClient", offline_client(handler))

    async def fast_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(login_module.asyncio, "sleep", fast_sleep)

    class Gate(asyncio.Event):
        rounds = 0

        async def wait(self):
            await real_sleep(0.02)
            self.rounds += 1
            if self.rounds >= 3:
                self.set()
            return True

    context = SimpleNamespace(
        get_config=lambda *args: {"admins_id": ["10001"]}, send_message=AsyncMock(return_value=True)
    )
    login = login_module.BilibiliLogin(tmp_path, context, Settings(), LOGGER, AsyncMock())
    login.state.update(
        status="expired",
        uid="1001",
        revision="expired-once",
        credential={"sessdata": "synthetic"},
        owner=login._owner(owner_event()),
    )
    login.stop = Gate()
    try:
        await login._monitor()
        if login.poll_task:
            await asyncio.wait_for(login.poll_task, 2)
        login.stop = Gate()
        login.next_notice_attempt = 0
        await login._monitor()
        pictures = [
            call for call in context.send_message.call_args_list if len(call.args[1].chain) == 2
        ]
        assert len(pictures) == 1
        assert pictures[0].args[0] == owner_event().unified_msg_origin
        assert login.state["notified_revision"] == "expired-once"
    finally:
        await login.close()


@pytest.mark.asyncio
async def test_five_routes_use_installed_sdk_contracts(monkeypatch):
    from astrbot_plugin_bilibili_shixun.services.bilibili_source import BilibiliSource
    from bilibili_api import Api, Credential, user

    observed = []

    async def request(api, **kwargs):
        observed.append((api.url, dict(api.params)))
        if "search/type" in api.url:
            return {"result": [video()]}
        if "arc/search" in api.url:
            return {"list": {"vlist": [video()]}}
        return {"items": [card()], "offset": "next-token", "has_more": True}

    monkeypatch.setattr(Api, "request", request)
    monkeypatch.setattr(user.User, "get_access_id", AsyncMock(return_value="offline"))
    login = SimpleNamespace(
        uid="1001",
        revision="revision",
        credentials=lambda: Credential(
            sessdata="synthetic", bili_jct="synthetic", buvid3="synthetic", dedeuserid="1001"
        ),
    )
    source = BilibiliSource(asyncio.get_running_loop(), login, LOGGER)
    for kind in (
        "keyword_search",
        "up_uploads",
        "up_dynamics",
        "following_videos",
        "following_dynamics",
    ):
        result = await source._fetch(
            {"kind": kind, "key": "1001" if kind != "keyword_search" else "codex"}, "1001", None, 10
        )
        assert len(result) == 1
    assert len(observed) == 5 and observed[0][1]["order"] == "pubdate"
    assert observed[-2][1]["type"] == "video" and observed[-1][1]["type"] == "all"


@pytest.mark.asyncio
async def test_astrbot_plugin_load_state_and_cleanup(tmp_path, monkeypatch):
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.star.context import Context
    from astrbot_plugin_bilibili_shixun import main

    context = object.__new__(Context)
    context._config = {"admins_id": ["10001"], "dashboard": {"username": "smoke-admin"}}
    context.provider_manager = SimpleNamespace(llm_tools=ToolSet())
    context.registered_web_apis = []
    context.llm_generate = AsyncMock(side_effect=AssertionError("No model call allowed"))
    monkeypatch.setattr(main, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    plugin = main.BilibiliShixunPlugin(context, {})
    info = Mock(wraps=plugin.logger.info)
    monkeypatch.setattr(plugin.logger, "info", info)
    try:
        await plugin.initialize()
        assert any(
            "registered %s tools" in call.args[0] and call.args[1] == 9
            for call in info.call_args_list
        )
        state = await plugin.application.web("state", {})
        assert state["bilibili_account"]["status"] == "not_logged_in"
        assert len(context.provider_manager.llm_tools.func_list) == 9
        from astrbot_plugin_bilibili_shixun.services.contracts import TOOL_NAMES

        assert {tool.name for tool in plugin.tools} == set(TOOL_NAMES.values())
        assert {tool.operation for tool in plugin.tools} == set(TOOL_NAMES)
        assert len(context.registered_web_apis) == 8
        assert context.llm_generate.await_count == 0
    finally:
        await plugin.terminate()
    assert not context.registered_web_apis and not context.provider_manager.llm_tools.func_list
    assert not plugin.application.tasks and not plugin.application.threads


@pytest.mark.asyncio
async def test_chat_refresh_permissions_replay_progress_and_retrieval(tmp_path, monkeypatch):
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.star.context import Context
    from astrbot_plugin_bilibili_shixun import main

    context = object.__new__(Context)
    context._config = {"admins_id": ["10001"]}
    context.astrbot_config_mgr = SimpleNamespace(get_conf=lambda umo: context._config)
    context.provider_manager = SimpleNamespace(llm_tools=ToolSet())
    context.registered_web_apis = []
    context.llm_generate = AsyncMock(side_effect=AssertionError("Refresh must not call a model"))
    monkeypatch.setattr(main, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    plugin = main.BilibiliShixunPlugin(context, {})
    event = owner_event()
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.is_admin = lambda: True
    event.message_obj = SimpleNamespace(message_id="refresh-message-1")
    entered, release, calls = asyncio.Event(), threading.Event(), []
    loop = asyncio.get_running_loop()

    def fetch(*args, **kwargs):
        calls.append(1)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "Test did not release simulated collection"
        return video_batch([video()])

    async def invoke(params=None):
        return json.loads(
            await plugin.call_tool(
                event, "refresh_sources", dict({"wait_seconds": 0}, **(params or {}))
            )
        )

    try:
        await plugin.initialize()
        assert (await invoke({"action": "status"}))["status"] == "not_started"
        assert (await invoke())["error_code"] == "NOT_CONFIGURED"
        await plugin.application.io(seed, plugin.application.runtime)
        event.is_private_chat = lambda: False
        assert (await invoke())["error_code"] == "ACCESS_DENIED"
        event.is_private_chat = lambda: True
        plugin.application.login.state.update(status="valid", uid="1001")
        plugin.application.source = SimpleNamespace(fetch=fetch)

        event.is_private_chat = lambda: False
        assert (await invoke())["error_code"] == "ACCESS_DENIED"
        event.is_private_chat = lambda: True
        assert (await invoke({"action": "unsupported"}))["error_code"] == "INVALID_ARGUMENT"

        first = await invoke()
        assert first["status"] == "running" and first["operation_finished"] is False
        await asyncio.wait_for(entered.wait(), 3)
        assert (await invoke({"action": "start"}))["id"] == first["id"]
        event.message_obj.message_id = "refresh-message-2"
        second = await invoke()
        assert second["id"] == first["id"] and second["reused_running"]
        assert (await invoke({"action": "status"}))["status"] == "running"

        release.set()
        await asyncio.wait_for(plugin.application.idle.wait(), 3)
        done = await invoke({"action": "status"})
        assert done["status"] == "completed" and done["operation_finished"]
        assert (await invoke())["id"] == first["id"]
        event.message_obj.message_id = "refresh-message-1"
        assert (await invoke())["status"] == "completed"
        found = json.loads(await plugin.call_tool(event, "search_items", {"terms": ["codex"]}))
        assert found["items"] and len(calls) == 1
        image_result = json.loads(
            await plugin.call_tool(event, "send_item_images", {"item_id": found["items"][0]["id"]})
        )
        assert image_result["error_code"] == "IMAGE_NOT_FOUND"
        assert context.llm_generate.await_count == 0

        await plugin.application.install_account("1002")
        assert (await invoke({"action": "status"}))["status"] == "not_started"
        assert (await invoke())["error_code"] == "REQUEST_CONFLICT"
    finally:
        release.set()
        await plugin.terminate()


def test_refresh_alias_survives_restart_without_duplicate_job(tmp_path):
    store = JobStore(tmp_path)
    store.initialize()
    first, _ = store.start("collect", "first-request", "1001")
    assert store.reuse_collection("second-request", first["id"], "1001")["id"] == first["id"]
    first.update(status="completed")
    store.finish(first)
    reloaded = JobStore(tmp_path)
    reloaded.initialize()
    job, created = reloaded.start("collect", "second-request", "1001")
    assert not created and job["id"] == first["id"] and job["status"] == "completed"
    with pytest.raises(PluginError):
        reloaded.start("collect", "second-request", "1002")
