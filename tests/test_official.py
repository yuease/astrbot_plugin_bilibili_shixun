"""Real AstrBot QQ event/codec paths with offline Bilibili and Tencent transports."""

import asyncio
import base64
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import botpy.message
import httpx
import pytest
from astrbot.core.message.components import Plain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.qqofficial.qqofficial_message_event import QQOfficialMessageEvent
from astrbot.core.platform.sources.qqofficial_webhook.qo_webhook_event import (
    QQOfficialWebhookMessageEvent,
)
from astrbot_plugin_bilibili_shixun.services import bilibili_login as login_module
from astrbot_plugin_bilibili_shixun.services import images as image_module
from astrbot_plugin_bilibili_shixun.services.access import authorize_chat, chat_identity
from astrbot_plugin_bilibili_shixun.services.errors import PluginError
from astrbot_plugin_bilibili_shixun.services.images import ImageDelivery
from astrbot_plugin_bilibili_shixun.services.qq_platform import (
    official_c2c,
    qq_message,
    qq_plain_result,
)
from astrbot_plugin_bilibili_shixun.services.settings import Settings
from test_issues import png
from test_smoke import LOGGER, offline_client
from test_users import invoke
from test_users import plugin as shared_plugin

plugin = shared_plugin  # Reuse the isolated real-host fixture.

ADAPTERS = ["qq_official", "qq_official_webhook"]


@pytest.fixture(autouse=True)
def no_host_telemetry(monkeypatch):
    from astrbot.core.utils.metrics import Metric

    monkeypatch.setattr(Metric, "upload", AsyncMock())


def official_event(adapter="qq_official", sender="OPENID_USER_A", *, failure=None, raw_kind="c2c"):
    captured = []

    async def request(route, **kwargs):
        payload = copy.deepcopy(kwargs["json"])
        captured.append((route.path, route.parameters, payload))
        if failure:
            failure(route.path, payload)
        if route.path.endswith("/files"):
            return {"file_uuid": "offline-file", "file_info": "offline-info", "ttl": 3600}
        return {"id": "outgoing-id", "timestamp": "2026-10-27T10:00:00Z"}

    api = SimpleNamespace(_http=SimpleNamespace(request=AsyncMock(side_effect=request)))
    payload = {
        "id": "incoming-id",
        "author": {"user_openid": sender, "id": sender},
        "content": "测试",
    }
    raw_cls = botpy.message.C2CMessage if raw_kind == "c2c" else botpy.message.DirectMessage
    raw = raw_cls(api=api, event_id=None, data=payload)
    message = AstrBotMessage()
    message.type = MessageType.FRIEND_MESSAGE
    message.message_id = "incoming-id"
    message.sender = MessageMember(sender, "离线用户")
    message.self_id = "offline-bot"
    message.group_id = ""
    message.message = [Plain("测试")]
    message.message_str = "测试"
    message.raw_message = raw
    cls = QQOfficialMessageEvent if adapter == "qq_official" else QQOfficialWebhookMessageEvent
    event = cls(
        "测试",
        message,
        PlatformMetadata(name=adapter, description="offline", id="official-instance"),
        sender,
        SimpleNamespace(api=api),
    )
    return event, captured


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_real_adapter_uploads_bytes_and_passive_message_with_plain_text(adapter):
    event, calls = official_event(adapter)
    data = png((30, 40))
    chain = qq_message(adapter, "图片说明", data)
    assert chain.use_markdown_ is False
    await event.send(chain)
    assert len(calls) == 2
    path, target, upload = calls[0]
    assert path == "/v2/users/{openid}/files" and target["openid"] == "OPENID_USER_A"
    assert base64.b64decode(upload["file_data"]) == data
    assert upload["srv_send_msg"] is False and "url" not in upload
    path, target, message = calls[1]
    assert path == "/v2/users/{openid}/messages" and target["openid"] == "OPENID_USER_A"
    assert message["msg_id"] == "incoming-id" and message["msg_type"] == 7
    assert message.get("markdown") is None and message["media"]["file_info"] == "offline-info"


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_real_event_accepts_openid_and_rejects_channel_dm_before_user_creation(
    plugin, adapter
):
    event, _ = official_event(adapter)
    current = await invoke(plugin, event, "get_settings")
    assert current["user_identity"]["sender"] == "OPENID_USER_A"
    assert current["platform_capabilities"]["user_id_kind"] == "openid"
    before = len(plugin.users.apps)
    dm, _ = official_event(adapter, "CHANNEL_USER", raw_kind="dm")
    denied = await invoke(plugin, dm, "get_settings")
    assert denied["error_code"] == "ACCESS_DENIED"
    assert len(plugin.users.apps) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_two_official_users_do_not_share_preferences_or_login_notices(plugin, adapter):
    a, _ = official_event(adapter)
    b, _ = official_event(adapter, "OPENID_USER_B")
    current = await invoke(plugin, a, "get_settings")
    changed = await invoke(
        plugin, a, "update_settings", expected_version=current["version"], add_keywords=["Codex"]
    )
    assert changed["settings_saved"]
    app_a = await plugin.users.chat(a)
    plugin.context._config["admins_id"].append("OPENID_USER_A")
    app_a.login.state.update(status="expired", uid="123", credential={"sessdata": "synthetic-only"})
    a_status = await invoke(plugin, a, "get_source_status")
    b_status = await invoke(plugin, b, "get_settings")
    assert "account_notice" in a_status
    assert "account_notice" not in b_status and b_status["settings"]["keywords"] == []
    assert "synthetic-only" not in json.dumps(a_status)


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("poll_code", [0, 86038])
async def test_qr_login_through_real_official_adapter(tmp_path, monkeypatch, adapter, poll_code):
    real_sleep = asyncio.sleep
    http_calls = []

    def response(request):
        http_calls.append(request.url.path)
        if request.url.path.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=offline",
                        "qrcode_key": "offline",
                    },
                },
            )
        if request.url.path.endswith("/poll"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"code": poll_code}},
                headers=[
                    ("Set-Cookie", "SESSDATA=synthetic-session; Domain=.bilibili.com; Path=/"),
                    ("Set-Cookie", "bili_jct=synthetic-csrf; Domain=.bilibili.com; Path=/"),
                ],
            )
        return httpx.Response(
            200, json={"code": 0, "data": {"isLogin": True, "mid": 1001, "uname": "离线账号"}}
        )

    monkeypatch.setattr(login_module.httpx, "AsyncClient", offline_client(response))

    async def fast_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr(login_module.asyncio, "sleep", fast_sleep)
    context = SimpleNamespace(get_config=lambda *args: {"admins_id": []}, send_message=AsyncMock())
    install = AsyncMock()
    login = login_module.BilibiliLogin(
        tmp_path, context, Settings(account_hosting_admin_only=False), LOGGER, install
    )
    event, outgoing = official_event(adapter)
    try:
        await login.request_login(event)
        await asyncio.wait_for(login.poll_task, 4)
        assert login_module.HOSTING_NOTICE in outgoing[0][2]["content"]
        assert outgoing[1][0].endswith("/files")
        assert base64.b64decode(outgoing[1][2]["file_data"]).startswith(b"\x89PNG")
        assert all(c[1]["openid"] == "OPENID_USER_A" for c in outgoing)
        assert all(c[2].get("markdown") is None for c in outgoing)
        assert context.send_message.await_count == 0
        if poll_code == 0:
            assert login.uid == "1001"
            assert login.public_state()["notification_mode"] == "on_use"
            assert not login.public_state()["proactive_supported"]
            install.assert_awaited_once_with("1001")
            await login.logout(event)
            assert (
                login.uid == ""
                and json.loads(login.path.read_text(encoding="utf-8"))["credential"] == {}
            )
        else:
            assert login.uid == "" and install.await_count == 0
        assert "synthetic-session" not in json.dumps(login.public_state())
    finally:
        await login.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_successful_login_is_not_rolled_back_when_result_delivery_fails(
    tmp_path, monkeypatch, adapter
):
    real_sleep = asyncio.sleep

    def response(request):
        if request.url.path.endswith("/generate"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web",
                        "qrcode_key": "offline",
                    },
                },
            )
        if request.url.path.endswith("/poll"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"code": 0}},
                headers=[
                    ("Set-Cookie", "SESSDATA=synthetic; Domain=.bilibili.com; Path=/"),
                    ("Set-Cookie", "bili_jct=synthetic; Domain=.bilibili.com; Path=/"),
                ],
            )
        return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 1001}})

    monkeypatch.setattr(login_module.httpx, "AsyncClient", offline_client(response))

    async def fast_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr(login_module.asyncio, "sleep", fast_sleep)

    def refuse(path, payload):
        if path.endswith("/messages") and "登录成功" in (payload.get("content") or ""):
            raise RuntimeError("simulated reply failure")

    event, calls = official_event(adapter, failure=refuse)
    login = login_module.BilibiliLogin(
        tmp_path,
        SimpleNamespace(get_config=lambda *args: {"admins_id": []}),
        Settings(account_hosting_admin_only=False),
        LOGGER,
        AsyncMock(),
    )
    try:
        await login.request_login(event)
        await asyncio.wait_for(login.poll_task, 4)
        assert login.uid == "1001" and login.public_state()["login_notice_failed"]
        assert json.loads(login.path.read_text(encoding="utf-8"))["status"] == "valid"
        assert not any("本次登录未完成" in (c[2].get("content") or "") for c in calls)
    finally:
        await login.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_official_expiry_monitor_never_pushes_qr(tmp_path, adapter):
    context = SimpleNamespace(get_config=lambda *args: {"admins_id": []}, send_message=AsyncMock())
    login = login_module.BilibiliLogin(
        tmp_path, context, Settings(account_hosting_admin_only=False), LOGGER, AsyncMock()
    )
    event, _ = official_event(adapter)
    login.state.update(
        status="expired",
        revision="once",
        credential={"sessdata": "synthetic"},
        owner=login._owner(event),
    )

    class Stop(asyncio.Event):
        async def wait(self):
            self.set()
            return True

    login.stop = Stop()
    login._begin = AsyncMock(side_effect=AssertionError("No official proactive login"))
    try:
        await login._monitor()
        assert context.send_message.await_count == 0 and login._begin.await_count == 0
    finally:
        await login.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_image_delivery_real_adapter_caption_and_no_duplicate_send(monkeypatch, adapter):
    monkeypatch.setattr(
        image_module.httpx,
        "AsyncClient",
        offline_client(lambda req: httpx.Response(200, content=png((20, 30)))),
    )
    event, calls = official_event(adapter)
    sender = ImageDelivery(
        SimpleNamespace(get_config=lambda *args: {"admins_id": []}),
        asyncio.to_thread,
        LOGGER,
        lambda: False,
        Settings(),
    )
    item = {
        "id": "item",
        "title": "作品 https://unconfigured.example/path",
        "author": "作者",
        "url": "https://t.bilibili.com/1",
        "images": ["https://i0.hdslb.com/bfs/new_dyn/offline.jpg"],
    }
    result = await sender.send(event, item, [0], "receipt")
    assert result["sent"] == 1 and len(calls) == 2
    assert result["status"] == "submitted" and result["delivery_confirmation"] == "unavailable"
    assert "http" not in calls[1][2]["content"]
    assert result["sent_details"][0]["delivery_adapter"] == adapter
    assert (await sender.send(event, item, [0], "receipt"))["replayed"]
    assert len(calls) == 2


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_native_command_result_does_not_require_markdown_permission(adapter):
    event, _ = official_event(adapter)
    assert qq_plain_result(event, "登录状态").use_markdown_ is False
    assert official_c2c(event) and chat_identity(event)["sender"] == "OPENID_USER_A"
    dm, _ = official_event(adapter, raw_kind="dm")
    with pytest.raises(PluginError):
        authorize_chat(dm, SimpleNamespace(get_config=lambda *a: {}), Settings())


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("fail_at", ["files", "messages"])
async def test_official_upload_or_message_failure_is_not_retried_by_plugin(
    monkeypatch, adapter, fail_at
):
    monkeypatch.setattr(
        image_module.httpx,
        "AsyncClient",
        offline_client(lambda req: httpx.Response(200, content=png((20, 30)))),
    )

    def fail(path, payload):
        if path.endswith("/" + fail_at):
            raise RuntimeError("simulated transport error")

    event, calls = official_event(adapter, failure=fail)
    sender = ImageDelivery(
        SimpleNamespace(get_config=lambda *a: {}),
        asyncio.to_thread,
        LOGGER,
        lambda: False,
        Settings(),
    )
    item = {
        "id": "one",
        "title": "作品",
        "author": "作者",
        "url": "https://t.bilibili.com/1",
        "images": ["https://i0.hdslb.com/bfs/new_dyn/a.jpg"],
    }
    result = await sender.send(event, item, [0], "failed-once")
    assert result["status"] == "failed" and result["sent"] == 0
    assert result["errors"][0]["error_code"] == "IMAGE_DELIVERY_UNCERTAIN"
    count = len(calls)
    assert (await sender.send(event, item, [0], "failed-once"))["replayed"]
    assert len(calls) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_owner_checks_prevent_sending_or_logging_out_other_user(tmp_path, adapter):
    a, calls_a = official_event(adapter)
    b, calls_b = official_event(adapter, "OPENID_USER_B")
    login = login_module.BilibiliLogin(
        tmp_path,
        SimpleNamespace(get_config=lambda *a: {}),
        Settings(account_hosting_admin_only=False),
        LOGGER,
        AsyncMock(),
    )
    login.state.update(
        status="valid", uid="1001", owner=login._owner(a), credential={"sessdata": "synthetic"}
    )
    try:
        with pytest.raises(PluginError) as error:
            await login._send(login._owner(a), "private QR", png((10, 10)), event=b)
        assert error.value.code == "ACCESS_DENIED"
        with pytest.raises(PluginError):
            await login.logout(b)
        assert login.uid == "1001" and not calls_a and not calls_b
        with pytest.raises(PluginError) as unavailable:
            await login._send(login._owner(a), "background", event=None)
        assert unavailable.value.code == "BILI_PROACTIVE_UNAVAILABLE"
    finally:
        await login.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_real_host_input_parser_preserves_c2c_openid(adapter):
    from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (
        QQOfficialPlatformAdapter,
    )

    event, _ = official_event(adapter)
    parsed = await QQOfficialPlatformAdapter._parse_from_qqofficial(
        event.message_obj.raw_message, MessageType.FRIEND_MESSAGE
    )
    assert isinstance(parsed.raw_message, botpy.message.C2CMessage)
    assert parsed.sender.user_id == "OPENID_USER_A" and not parsed.group_id


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_account_command_runs_without_model_and_uses_plain_result(plugin, adapter):
    event, _ = official_event(adapter)
    replies = [value async for value in plugin._account_command(event, "status")]
    assert len(replies) == 1 and replies[0].use_markdown_ is False
    assert "尚未登录" in replies[0].get_plain_text()
    assert event.unified_msg_origin in replies[0].get_plain_text()
    assert plugin.context.llm_generate.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_silent_host_send_failure_never_claims_confirmed_delivery(monkeypatch, adapter):
    monkeypatch.setattr(
        image_module.httpx,
        "AsyncClient",
        offline_client(lambda req: httpx.Response(200, content=png((20, 30)))),
    )
    event, calls = official_event(adapter)
    original = event.bot.api._http.request.side_effect

    async def response(route, **kwargs):
        if route.path.endswith("/messages"):
            return []  # The pinned host logs this and returns no delivery receipt.
        return await original(route, **kwargs)

    event.bot.api._http.request.side_effect = response
    sender = ImageDelivery(
        SimpleNamespace(get_config=lambda *a: {}),
        asyncio.to_thread,
        LOGGER,
        lambda: False,
        Settings(),
    )
    item = {
        "id": "one",
        "title": "作品",
        "author": "作者",
        "url": "https://t.bilibili.com/1",
        "images": ["https://i0.hdslb.com/bfs/new_dyn/a.jpg"],
    }
    result = await sender.send(event, item, [0], "silent-failure")
    assert result["status"] == "submitted" and result["delivery_confirmation"] == "unavailable"
    assert "不等于确认用户收到" in result["guidance"]
    assert event.bot.api._http.request.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_repeated_official_login_reuses_live_qr_without_claiming_delivery(
    tmp_path, monkeypatch, adapter
):
    generates = []

    def response(request):
        if request.url.path.endswith("/generate"):
            generates.append(1)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web",
                        "qrcode_key": "offline",
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {"code": 86101}})

    monkeypatch.setattr(login_module.httpx, "AsyncClient", offline_client(response))
    event, calls = official_event(adapter)
    login = login_module.BilibiliLogin(
        tmp_path,
        SimpleNamespace(get_config=lambda *a: {}),
        Settings(account_hosting_admin_only=False),
        LOGGER,
        AsyncMock(),
    )
    try:
        first = await login.request_login(event)
        pending = login.pending
        second = await login.request_login(event)
        assert login.pending is pending and len(generates) == 1
        assert "已提交" in first and "重新提交" in second
        assert "已发送" not in first + second
        assert len([c for c in calls if c[0].endswith("/files")]) == 2
    finally:
        await login.close()
