"""Regression scenarios from the real chat issue report; all transport is simulated."""

import asyncio
import base64
import json
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from astrbot_plugin_bilibili_shixun.core import query as query_module
from astrbot_plugin_bilibili_shixun.core.bilibili_items import dynamic_batch
from astrbot_plugin_bilibili_shixun.core.core_errors import CoreError
from astrbot_plugin_bilibili_shixun.core.schemas import Article
from astrbot_plugin_bilibili_shixun.services import images as image_module
from astrbot_plugin_bilibili_shixun.services import runtime as runtime_module
from astrbot_plugin_bilibili_shixun.services.contracts import SCHEMAS, tool_specs
from astrbot_plugin_bilibili_shixun.services.errors import PluginError, log_failure
from astrbot_plugin_bilibili_shixun.services.images import (
    ImageDelivery,
    download_image,
    original_image,
    resize_image,
)
from astrbot_plugin_bilibili_shixun.services.runtime import Runtime
from astrbot_plugin_bilibili_shixun.services.settings import Settings
from PIL import Image
from test_smoke import LOGGER, Model, offline_client, owner_event, seed

NOW = datetime(2026, 10, 26, 19, tzinfo=ZoneInfo("Asia/Shanghai"))


def word(index=1, *, body=None, stamp=NOW):
    return {
        "id_str": str(80000 + index),
        "type": "DYNAMIC_TYPE_WORD",
        "modules": {
            "module_author": {"mid": 2002, "name": "原作者", "pub_ts": int(stamp.timestamp())},
            "module_dynamic": {"desc": {"text": body or f"研究进展编号 {index}"}},
        },
    }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(query_module, "local_now", lambda: NOW)
    monkeypatch.setattr(runtime_module, "local_now", lambda: NOW)
    instance = Runtime(tmp_path, Settings())
    seed(instance)
    state = instance.state()
    plan = state["profile"]["source_plan"]
    plan["keyword_search"]["enabled"] = False
    plan["following"].update(enabled=True, uid="1001", scope="dynamics")
    instance.save_profile(
        {
            "expected_version": state["profile_version"],
            "changes": {"source_plan": plan},
            "request_id": "following",
        }
    )
    return instance


def collect(runtime, cards, *, has_more=False, run_id="issue-collect"):
    batch = dynamic_batch({"items": cards, "has_more": has_more, "offset": "next"})
    return runtime.run_job(
        "collect", run_id, SimpleNamespace(fetch=lambda *args, **kwargs: batch), Model()
    )


def test_browse_following_today_without_content_keywords(runtime):
    collect(runtime, [word(i, body="关注方法" if i == 1 else None) for i in range(1, 10)])
    query = {
        "question": "我关注的UP今天更新了什么",
        "time_range": {"period": "today"},
        "sources": [{"kind": "following_dynamics", "key": "1001"}],
        "limit": 10,
    }
    result = runtime.tool("search_items", query)
    assert result["total_matches"] == 9 and len(result["items"]) == 9
    assert result["retrieval_terms"] == [] and result["metrics"]["logical_model_calls"] == 0
    literal = runtime.tool("search_items", dict(query, terms=["关注"]))
    assert literal["total_matches"] == 1
    assert "query_updates" not in SCHEMAS
    assert len([op for op, _, _ in tool_specs() if op in ("search_items", "query_updates")]) == 1


def test_compact_results_do_not_repeat_evidence_or_claim_global_completeness(runtime):
    collect(runtime, [word(i) for i in range(1, 10)])
    result = runtime.tool("search_items", {"limit": 10})
    encoded = json.dumps(result, ensure_ascii=False)
    assert len(encoded) < 7000
    assert "snippet_evidence" not in encoded and "matched_published_at" not in encoded
    assert "dynamic_contexts" not in encoded and "truncated" not in result
    assert result["coverage"]["completeness"] == "unknown"
    assert "insufficient" not in result["coverage"]
    assert "retained_sampling" in result["coverage"]["reasons"]
    assert result["coverage"]["missing_publication_time_count"] == 0
    assert result["coverage"]["sources"][0]["scan"]["pagination_exhausted"]


def test_budget_stop_remains_visible_despite_ready_source(runtime):
    state = runtime.state()
    collection = state["profile"]["collection"]
    collection["dynamic_batches"] = 1
    runtime.save_profile(
        {
            "expected_version": state["profile_version"],
            "changes": {"collection": collection},
            "request_id": "one-batch",
        }
    )
    collect(runtime, [word()], has_more=True)
    result = runtime.tool("search_items", {})
    scope = result["coverage"]
    assert scope["completeness"] == "incomplete"
    assert scope["sources"][0]["availability"] == "ready"
    assert scope["sources"][0]["scan"]["stop_reason"] == "batch_limit"
    assert not scope["sources"][0]["scan"]["pagination_exhausted"]


def test_pagination_and_snippet_truncation_are_independent(runtime):
    collect(runtime, [word(1, body="非常长的研究内容" * 300), word(2)])
    first = runtime.tool("search_items", {"limit": 1})
    assert first["has_more"] and first["next_page"] == 2 and first["total_matches"] == 2
    second = runtime.tool(
        "search_items", {"limit": 1, "page": 2, "snapshot_version": first["snapshot_version"]}
    )
    assert not second["has_more"] and second["next_page"] is None
    all_items = runtime.tool("search_items", {"limit": 10})
    assert not all_items["has_more"] and "truncated" not in all_items
    assert any(item["snippet_truncated"] for item in all_items["items"])
    short = next(item for item in all_items["items"] if "编号 2" in item["title"])
    assert short["snippet_truncated"] is False
    long = next(item for item in all_items["items"] if "非常长" in item["title"])
    detail = runtime.tool("get_item", {"item_id": long["id"], "max_body_chars": 100})
    assert detail["body_truncated"] and len(detail["body"]) <= 100
    assert "snippet" not in detail and "snippet_evidence" not in detail


def test_today_repost_keeps_old_original_and_correct_text_attribution(runtime):
    original = word(1, body="原文研究结论", stamp=NOW - timedelta(days=3))
    forwarded = word(2, body="转发者独立观点")
    forwarded["type"] = "DYNAMIC_TYPE_FORWARD"
    forwarded["modules"]["module_author"].update(mid=3003, name="转发者")
    forwarded["orig"] = original
    collect(runtime, [forwarded])
    result = runtime.tool(
        "search_items", {"terms": ["转发者独立观点"], "time_range": {"period": "today"}}
    )
    item = result["items"][0]
    assert item["activity_type"] == "repost" and item["forwarded_by"] == "转发者"
    assert item["author"] == "原作者" and item["snippet_attribution"]["author"] == "转发者"
    assert item["published_at"].startswith("2026-10-26")
    assert item["original_published_at"].startswith("2026-10-23")
    assert item["url"].endswith("80001") and item["activity_url"].endswith("80002")
    detail = runtime.tool("get_item", {"item_id": item["id"]})
    assert detail["body"].count("原文研究结论") == 1
    authors = {part["author"] for part in detail["attributions"]}
    assert authors == {"转发者", "原作者"}
    for part in detail["attributions"]:
        if part["role"] == "repost_comment":
            assert detail["body"][part["body_start"] : part["body_end"]] == "转发者独立观点"


@pytest.mark.parametrize("timezone, offset", [("Asia/Shanghai", "+08:00"), ("UTC", "+00:00")])
def test_all_tool_timestamps_use_requested_timezone(runtime, timezone, offset):
    collect(runtime, [word()])
    result = runtime.tool("search_items", {"time_range": {"period": "today", "timezone": timezone}})
    detail = runtime.tool("get_item", {"item_id": result["items"][0]["id"], "timezone": timezone})
    status = runtime.tool("get_source_status", {"timezone": timezone})

    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if child is not None and (key.endswith("_at") or key in {"start", "end", "as_of"}):
                    assert child.endswith(offset), (key, child)
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    for output in (result, detail, status):
        check(output)
    with pytest.raises(CoreError):
        runtime.tool("get_item", {"item_id": result["items"][0]["id"], "timezone": "invalid/zone"})


def test_image_dynamic_label_and_no_empty_repost_context(runtime):
    original = word(1, body="图片配文")
    original["type"] = "DYNAMIC_TYPE_DRAW"
    original["modules"]["module_dynamic"]["major"] = {
        "draw": {"items": [{"src": "https://i0.hdslb.com/bfs/new_dyn/sample.jpg"}]}
    }
    collect(runtime, [original])
    result = runtime.tool("search_items", {"content_types": ["image_dynamic"]})
    assert result["items"][0]["content_type"] == "image_dynamic"
    detail = runtime.tool("get_item", {"item_id": result["items"][0]["id"]})
    assert detail["image_count"] == 1 and detail["body"] == "图片配文"
    original["modules"]["module_dynamic"]["desc"]["text"] = ""
    outer = word(2)
    outer.update(type="DYNAMIC_TYPE_FORWARD", orig=original)
    item = dynamic_batch({"items": [outer]})[0]
    assert all(context["text"] for context in item.dynamic_contexts)
    assert item.original_published_at and item.author == "原作者"


def test_today_window_excludes_next_midnight_and_reports_missing_time(runtime):
    cards = [
        word(1, stamp=NOW.replace(hour=0, minute=1)),
        word(2, stamp=(NOW + timedelta(days=1)).replace(hour=0, minute=0)),
        word(3),
    ]
    cards[2]["modules"]["module_author"]["pub_ts"] = 0
    collect(runtime, cards)
    result = runtime.tool("search_items", {"time_range": {"period": "today"}})
    assert result["total_matches"] == 1
    assert result["items"][0]["published_at"].startswith("2026-10-26T00:01")
    assert result["coverage"]["missing_publication_time_count"] == 1
    assert "missing_publication_time" in result["coverage"]["reasons"]


def png(size=(1800, 2200)):
    output = BytesIO()
    Image.new("RGB", size, "orange").save(output, "PNG")
    return output.getvalue()


def test_images_resize_and_reject_invalid_or_oversized_pixels(monkeypatch):
    result = resize_image(png())
    with Image.open(BytesIO(result)) as image:
        assert image.width <= 1080 and image.height <= 1620 and image.format == "JPEG"
    assert len(result) < image_module.MAX_OUTPUT
    with pytest.raises(PluginError):
        resize_image(b"not an image")
    monkeypatch.setattr(image_module, "MAX_PIXELS", 100)
    with pytest.raises(PluginError) as error:
        resize_image(png((20, 20)))
    assert error.value.code == "IMAGE_TOO_LARGE"


@pytest.mark.asyncio
async def test_image_download_limits_headers_and_redirects(monkeypatch):
    requests = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"123"
            yield b"456"

    def handler(request):
        requests.append(request)
        assert "cookie" not in request.headers
        assert request.headers["referer"] == "https://www.bilibili.com/"
        if request.url.path.endswith("redirect.jpg"):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
        if request.url.path.endswith("stream.jpg"):
            return httpx.Response(200, stream=Chunks())
        return httpx.Response(200, content=b"123456")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=image_module.HEADERS, follow_redirects=False
    ) as client:
        with pytest.raises(PluginError):
            await download_image(client, "http://127.0.0.1/private")
        assert not requests
        with pytest.raises(httpx.HTTPStatusError):
            await download_image(client, "https://i0.hdslb.com/bfs/new_dyn/redirect.jpg")
        assert len(requests) == 1
        monkeypatch.setattr(image_module, "MAX_DOWNLOAD", 5)
        with pytest.raises(PluginError) as error:
            await download_image(client, "https://i0.hdslb.com/bfs/new_dyn/large.jpg")
        assert error.value.code == "IMAGE_TOO_LARGE"
        with pytest.raises(PluginError) as error:
            await download_image(client, "https://i0.hdslb.com/bfs/new_dyn/stream.jpg")
        assert error.value.code == "IMAGE_TOO_LARGE"


@pytest.mark.asyncio
async def test_image_send_partial_failure_and_same_request_replay(monkeypatch):
    count = []

    def handler(request):
        count.append(1)
        if request.url.path.endswith("bad.jpg"):
            return httpx.Response(412)
        return httpx.Response(200, content=png((30, 40)))

    monkeypatch.setattr(image_module.httpx, "AsyncClient", offline_client(handler))
    context = SimpleNamespace(get_config=lambda *args: {"admins_id": ["10001"]})
    event = owner_event()
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.is_admin = lambda: True
    sender = ImageDelivery(context, asyncio.to_thread, LOGGER, lambda: False)
    item = {
        "id": "example",
        "title": "作品",
        "author": "作者",
        "url": "https://t.bilibili.com/1",
        "images": [
            "https://i0.hdslb.com/bfs/new_dyn/good.jpg",
            "https://i0.hdslb.com/bfs/new_dyn/bad.jpg",
        ],
    }
    first = await sender.send(event, item, [0, 1], "same-message")
    assert first["status"] == "partial" and first["sent"] == 1
    assert first["errors"][0]["index"] == 1
    assert event.send.await_count == 1
    assert event.send.call_args.args[0].chain[1].file.startswith("base64://")
    replayed = await sender.send(event, item, [0, 1], "same-message")
    assert replayed["replayed"] and len(count) == 2 and event.send.await_count == 1
    event.send.side_effect = RuntimeError("delivery confirmation lost")
    uncertain = await sender.send(event, item, [0, 1], "uncertain-message")
    assert (
        uncertain["sent"] == 0
        and uncertain["errors"][0]["error_code"] == "IMAGE_DELIVERY_UNCERTAIN"
    )
    assert event.send.await_count == 2
    await sender.send(event, item, [0, 1], "uncertain-message")
    assert event.send.await_count == 2


def test_legacy_record_keeps_repost_attribution_and_safe_log_locations(caplog):
    original = word(1, stamp=NOW - timedelta(days=3))
    forwarded = word(2)
    forwarded["modules"]["module_author"].update(mid=3003, name="转发者")
    forwarded.update(type="DYNAMIC_TYPE_FORWARD", orig=original)
    old = dynamic_batch({"items": [forwarded]})[0].model_dump()
    for key in ("activity_type", "activity_url", "forwarded_by", "original_published_at"):
        old.pop(key)
    restored = Article.model_validate(old)
    assert restored.activity_type == "repost" and restored.forwarded_by == "转发者"
    assert restored.original_published_at
    try:
        raise RuntimeError("synthetic_cookie_secret")
    except RuntimeError as exc:
        log_failure(LOGGER, "initialize", "startup", exc)
    assert "locations=" in caplog.text and "synthetic_cookie_secret" not in caplog.text


def poll_card(index=1, *, options=True):
    value = word(index, body="[UPOWER_6757716_卖萌]后面拍什么")
    value["type"] = "DYNAMIC_TYPE_DRAW"
    content = value["modules"]["module_dynamic"]
    content["major"] = {"type": "MAJOR_TYPE_OPUS", "opus": {"pics": []}}
    content["additional"] = {
        "type": "ADDITIONAL_TYPE_VOTE",
        "vote": {"vote_id": 42 + index, "desc": "后面拍什么？"},
    }
    if options:
        content["additional"]["vote"]["options"] = [{"desc": "舞台"}, {"desc": "日常"}]
    return value


def test_poll_is_not_image_and_list_can_filter_real_image_count(runtime):
    poll = poll_card()
    gallery = word(2)
    gallery["type"] = "DYNAMIC_TYPE_DRAW"
    gallery["modules"]["module_dynamic"]["major"] = {
        "draw": {"items": [{"src": "https://i0.hdslb.com/bfs/new_dyn/one.jpg"}]}
    }
    collect(runtime, [poll, gallery, word(3)])
    items = runtime.tool("search_items", {"limit": 10})["items"]
    vote_item = next(item for item in items if "卖萌" in item["title"])
    assert vote_item["content_type"] == "image_dynamic" and vote_item["image_count"] == 0
    assert vote_item["images_status"] == "known" and vote_item["attachment_types"] == ["poll"]
    assert vote_item["has_attachments"] is True and vote_item["attachments_status"] == "known"
    detail = runtime.tool("get_item", {"item_id": vote_item["id"]})
    assert detail["body"].startswith(":卖萌:") and "UPOWER_" not in detail["body"]
    assert detail["attachments"][0]["question"] == "后面拍什么？"
    assert [option["text"] for option in detail["attachments"][0]["options"]] == ["舞台", "日常"]
    with_images = runtime.tool("search_items", {"has_images": True})
    assert with_images["total_matches"] == 1 and with_images["items"][0]["image_count"] == 1
    assert runtime.tool("search_items", {"has_images": False})["total_matches"] == 2


def test_old_attachment_metadata_is_unknown_until_refreshed(runtime):
    card = poll_card()
    old = dynamic_batch({"items": [card]})[0].model_dump()
    for key in ("attachments", "attachments_status", "images_status"):
        old.pop(key)
    runtime.run_job(
        "collect",
        "legacy-attachments",
        SimpleNamespace(fetch=lambda *args, **kwargs: [old]),
        Model(),
    )
    item = runtime.tool("search_items", {})["items"][0]
    assert item["image_count"] == 0 and item["images_status"] == "unknown"
    assert item["has_attachments"] is None
    assert runtime.tool("search_items", {"has_images": False})["total_matches"] == 0
    collect(runtime, [card], run_id="updated-attachments")
    item = runtime.tool("search_items", {})["items"][0]
    assert item["attachment_types"] == ["poll"] and item["images_status"] == "known"


def test_missing_poll_options_and_unknown_attachment_are_explicit(runtime):
    card = poll_card(options=False)
    collect(runtime, [card])
    item = runtime.tool("search_items", {})["items"][0]
    detail = runtime.tool("get_item", {"item_id": item["id"]})
    assert detail["attachments"][0]["options"] == []
    assert detail["attachments"][0]["details_status"] == "partial"
    unknown = word(2)
    unknown["modules"]["module_dynamic"]["additional"] = {
        "type": "ADDITIONAL_TYPE_FUTURE",
        "secret": "must not persist",
    }
    parsed = dynamic_batch({"items": [unknown]})[0]
    assert parsed.attachments[0]["type"] == "unknown"
    assert parsed.attachments_status == "partial" and "must not persist" not in json.dumps(
        parsed.attachments
    )


@pytest.mark.asyncio
async def test_sdk_poll_enrichment_is_bounded_and_failure_preserves_feed(monkeypatch):
    from astrbot_plugin_bilibili_shixun.services.bilibili_source import BilibiliSource
    from bilibili_api import Api, Credential

    requests = []
    fail = False

    async def request(api, **kwargs):
        requests.append(api.url)
        if "vote_info" in api.url:
            if fail:
                raise RuntimeError("poll unavailable")
            return {"info": {"title": "实际投票问题", "options": [{"desc": "甲"}, {"desc": "乙"}]}}
        return {"items": [poll_card(i, options=False) for i in range(1, 4)], "has_more": False}

    monkeypatch.setattr(Api, "request", request)
    login = SimpleNamespace(
        uid="1001",
        revision="first",
        credentials=lambda: Credential(sessdata="synthetic"),
        require_hosting=lambda: None,
    )
    source = BilibiliSource(asyncio.get_running_loop(), login, LOGGER)
    first = await source._fetch({"kind": "following_dynamics", "key": "1001"}, "1001", None, 10)
    assert len(first) == 3 and sum("vote_info" in url for url in requests) == 2
    assert first[0].attachments[0]["details_status"] == "complete"
    assert first[2].attachments[0]["details_status"] == "partial"
    fail = True
    login.uid = "1002"
    second = await source._fetch({"kind": "following_dynamics", "key": "1002"}, "1002", None, 10)
    assert len(second) == 3 and second[0].attachments[0]["details_status"] == "unavailable"


@pytest.mark.asyncio
async def test_original_image_is_not_resized_and_preview_is_explicit(monkeypatch):
    raw = png()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, content=raw)

    monkeypatch.setattr(image_module.httpx, "AsyncClient", offline_client(handler))
    context = SimpleNamespace(get_config=lambda *args: {"admins_id": ["10001"]})
    event = owner_event()
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.is_admin = lambda: True
    sender = ImageDelivery(context, asyncio.to_thread, LOGGER, lambda: False)
    item = {
        "id": "original",
        "title": "原图",
        "author": "作者",
        "url": "https://t.bilibili.com/1",
        "images": ["https://i0.hdslb.com/bfs/new_dyn/original.png@200w_200h.webp"],
    }
    result = await sender.send(event, item, [0], "original-message")
    sent_bytes = base64.b64decode(
        event.send.call_args.args[0].chain[1].file.removeprefix("base64://")
    )
    assert sent_bytes == raw and result["sent_details"][0]["original_bytes_preserved"]
    assert paths[-1].endswith("original.png")
    result = await sender.send(event, item, [0], "preview-message", "preview")
    sent_bytes = base64.b64decode(
        event.send.call_args.args[0].chain[1].file.removeprefix("base64://")
    )
    with Image.open(BytesIO(sent_bytes)) as image:
        assert image.format == "JPEG" and image.width <= 1080 and image.height <= 1620
    assert (
        result["quality"] == "preview" and not result["sent_details"][0]["original_bytes_preserved"]
    )
    assert "@200w" in paths[-1]


def test_original_rejects_excessive_pixels_without_silent_resize(monkeypatch):
    raw = png((20, 20))
    assert original_image(raw) == raw
    monkeypatch.setattr(image_module, "MAX_PIXELS", 100)
    with pytest.raises(PluginError) as error:
        original_image(raw)
    assert error.value.code == "IMAGE_TOO_LARGE"
