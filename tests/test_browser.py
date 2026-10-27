"""Real Chromium page against isolated plugin handlers; only host auth/bridge is simulated."""

import asyncio
import json
import os
import socket
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import uvicorn
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot_plugin_bilibili_shixun.core.bilibili_items import video_batch
from astrbot_plugin_bilibili_shixun.services.settings import PLUGIN_NAME
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from test_official import official_event
from test_smoke import Model, video
from test_users import invoke
from test_users import plugin as shared_plugin

plugin = shared_plugin
ROOT = Path(__file__).resolve().parents[1]

BRIDGE = """
window.AstrBotPluginView = {
 ready: async () => {},
 apiGet: async (endpoint) => {
   const r=await fetch('/backend/'+endpoint); const d=await r.json();
   if(!r.ok) throw new Error(d.message||'request failed'); return d;
 },
 apiPost: async (endpoint, body) => {
   const r=await fetch('/backend/'+endpoint,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
   const d=await r.json(); if(!r.ok) throw new Error(d.message||'request failed'); return d;
 },
 download: async endpoint => {await fetch('/backend/'+endpoint);}
};
"""


@pytest.mark.asyncio
async def test_chromium_gui_settings_collection_feedback_and_user_isolation(plugin):
    playwright = pytest.importorskip("playwright.async_api")
    app = FastAPI()

    @app.api_route("/backend/{endpoint:path}", methods=["GET", "POST"])
    async def backend(endpoint: str, request: Request):
        wrapped = PluginRequest(
            request, plugin_name=PLUGIN_NAME, username=request.cookies.get("test_identity")
        )
        with bind_request_context(wrapped):
            operation = {
                "state": "state",
                "profile": "profile",
                "collect": "collect",
                "generate": "generate",
                "feedback": "feedback",
                "undo": "undo",
            }.get(endpoint)
            if operation:
                return await plugin._web(operation)
            if endpoint.startswith("runs/"):
                pieces = endpoint.split("/")
                return await plugin._web("download" if len(pieces) == 3 else "detail", pieces[1])
            raise AssertionError("Unexpected browser endpoint")

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "views/bilibili-shixun/index.html")

    app.mount("/", StaticFiles(directory=ROOT / "views/bilibili-shixun"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(8):
            while not server.started:
                await asyncio.sleep(0.01)
        base = f"http://127.0.0.1:{port}"
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                channel=os.environ.get("SHIXUN_BROWSER_CHANNEL")
                or (
                    "msedge"
                    if sys.platform == "win32" and not Path(p.chromium.executable_path).exists()
                    else None
                ),
                args=["--disable-background-networking"],
            )
            try:
                context = await browser.new_context(viewport={"width": 1440, "height": 1050})
                await context.add_cookies(
                    [{"name": "test_identity", "value": "admin", "url": base}]
                )
                await context.add_init_script(BRIDGE)
                await context.route(
                    "**/*",
                    lambda route: (
                        route.continue_()
                        if urlsplit(route.request.url).hostname == "127.0.0.1"
                        else route.abort()
                    ),
                )
                page = await context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(base)
                await playwright.expect(page.locator("#bilibili-account-status")).to_contain_text(
                    "此档案未获账号托管权限"
                )
                await page.locator('[data-view="sources"]').first.click()
                await playwright.expect(page.locator("#save-sources")).to_be_enabled()
                await page.locator("#source-keyword-enabled").check()
                await page.locator("#source-keywords").fill("Codex")
                await page.locator("#save-sources").click()
                await playwright.expect(page.locator("#toast")).to_contain_text("设置已应用")
                gui = await plugin.users.dashboard("admin")
                assert (await gui.tool("get_settings", {}))["settings"]["keywords"] == ["Codex"]
                gui.source.fetch = lambda *args, **kwargs: video_batch([video()])
                gui.model = Model()
                await page.locator('[data-view="briefing"]').first.click()
                await page.locator("#preview").click()
                await playwright.expect(page.locator("#pool-status")).to_contain_text(
                    "1 条有效候选", timeout=15000
                )
                await page.locator("#generate").click()
                await playwright.expect(page.locator("#briefing-body")).to_contain_text(
                    "离线样本摘要", timeout=15000
                )
                await page.locator('[data-feedback="useful"]').click()
                await playwright.expect(page.locator("#feedback-count")).to_have_text("1")
                await page.locator("[data-undo]").click()
                await playwright.expect(page.locator("#feedback-count")).to_have_text("0")
                run_id = (await gui.io(gui.runtime.state))["runs"][0]["id"]
                report = await page.request.get(base + "/backend/runs/" + run_id + "/download")
                assert report.status == 200 and "离线样本摘要" in await report.text()

                editor = await browser.new_context()
                await editor.add_cookies(
                    [{"name": "test_identity", "value": "editor", "url": base}]
                )
                other = await editor.request.get(base + "/backend/state")
                other_data = await other.json()
                assert other_data["profile"]["keywords"] == [] and not other_data["runs"]
                denied = await editor.request.get(base + "/backend/runs/" + run_id + "/download")
                assert denied.status != 200
                assert (
                    await page.request.get(base + "/backend/state?user_id=foreign")
                ).status == 400
                outsider = await browser.new_context()
                assert (await outsider.request.get(base + "/backend/state")).status == 403

                who, _ = official_event()
                original = await invoke(plugin, who, "get_settings")
                await invoke(
                    plugin,
                    who,
                    "update_settings",
                    expected_version=original["version"],
                    add_keywords=["AstrBot"],
                )
                plugin.users.settings = replace(
                    plugin.settings,
                    dashboard_bindings=("admin|qq_official|official-instance|OPENID_USER_A",),
                )
                await page.reload()
                await page.locator('[data-view="sources"]').first.click()
                await playwright.expect(page.locator("#source-keywords")).to_have_value("AstrBot")
                await page.locator("#source-keywords").fill("unsaved-draft")
                current = await invoke(plugin, who, "get_settings")
                await invoke(
                    plugin,
                    who,
                    "update_settings",
                    expected_version=current["version"],
                    add_keywords=["LangGraph"],
                )
                await playwright.expect(page.locator("#sources-message")).to_contain_text(
                    "草稿已保留", timeout=12000
                )
                await playwright.expect(page.locator("#source-keywords")).to_have_value(
                    "unsaved-draft"
                )
                await page.locator('[data-reload-settings="sources"]').click()
                await playwright.expect(page.locator("#source-keywords")).to_have_value(
                    "AstrBot, LangGraph"
                )
                output = ROOT / ".dev/verification-v0.9.1"
                output.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(output / "gui-sources.png"), full_page=True)
                (output / "browser.json").write_text(
                    json.dumps(
                        {
                            "engine": "chromium",
                            "version": browser.version,
                            "local_transport_only": True,
                            "host_auth_and_bridge": "simulated",
                            "plugin_handlers": "real",
                        }
                    ),
                    encoding="utf-8",
                )
                assert not errors, errors
                assert await page.get_by_role("button", name="扫码登录").count() == 0
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)
        sock.close()
