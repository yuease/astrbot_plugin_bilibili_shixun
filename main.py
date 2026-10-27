"""Thin AstrBot entry points over shared application services."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

import jsonschema
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request, stream_response
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from pydantic import Field, ValidationError
from pydantic.dataclasses import dataclass

from .core.core_errors import CoreError
from .core.library import RequestError
from .services.access import ReadBudget, authorize_chat, authorize_web, chat_request_id
from .services.application import Application
from .services.contracts import (
    KINDS,
    SCHEMAS,
    TOOL_NAMES,
    public_description,
    public_guidance,
    tool_specs,
)
from .services.errors import PluginError, log_failure
from .services.settings import MAX_BODY_BYTES, PLUGIN_NAME, Settings

IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


@dataclass
class BilibiliShixunTool(FunctionTool[AstrAgentContext]):
    operation: str = ""
    plugin: Any = Field(default=None, exclude=True, repr=False)

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return await self.plugin.call_tool(context.context.event, self.operation, kwargs)


class BilibiliShixunPlugin(Star):
    name = PLUGIN_NAME

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.settings = Settings.from_config(config)
        root = Path(get_astrbot_plugin_data_path()).resolve() / PLUGIN_NAME / "runtime-direct-v1"
        self.application = Application(root, context, self.settings, self.logger)
        self.budget = ReadBudget()
        self._terminated = False
        self._close_task = None
        self._routes = []
        self.tools = [
            BilibiliShixunTool(
                name=TOOL_NAMES[operation],
                description=public_description(description),
                parameters=schema,
                operation=operation,
                plugin=self,
            )
            for operation, schema, description in tool_specs()
        ]
        if {tool.name for tool in self.tools} & {
            tool.name for tool in context.get_llm_tool_manager().func_list
        }:
            raise RuntimeError("B站拾讯工具名已注册，请先卸载旧插件实例。")
        try:
            context.add_llm_tools(*self.tools)
            for endpoint, methods, handler in (
                ("state", ["GET"], self.web_state),
                ("runs/<run_id>", ["GET"], self.web_detail),
                ("runs/<run_id>/download", ["GET"], self.web_download),
                ("collect", ["POST"], self.web_collect),
                ("generate", ["POST"], self.web_generate),
                ("profile", ["POST"], self.web_profile),
                ("feedback", ["POST"], self.web_feedback),
                ("undo", ["POST"], self.web_undo),
            ):
                route = f"/{PLUGIN_NAME}/{endpoint}"
                context.register_web_api(route, handler, methods, "B站拾讯管理员页面")
                self._routes.append((route, handler, tuple(methods)))
        except Exception as exc:
            log_failure(self.logger, "register_tools_and_routes", "startup", exc)
            self._unregister()
            raise

    async def initialize(self):
        try:
            await self.application.start()
        except Exception as exc:
            log_failure(self.logger, "initialize", "startup", exc)
            raise
        self.logger.info(
            "B站拾讯 v0.7.0 initialized; registered %s tools: %s",
            len(self.tools),
            ", ".join(tool.name for tool in self.tools),
        )

    async def call_tool(self, event, operation, params):
        key = uuid.uuid4().hex
        try:
            authorize_chat(event, self.context)
            if operation not in SCHEMAS:
                raise PluginError("INVALID_ARGUMENT")
            if (
                isinstance(params, dict)
                and isinstance(params.get("sources"), list)
                and any(
                    isinstance(s, dict)
                    and isinstance(s.get("kind"), str)
                    and s["kind"] not in KINDS
                    for s in params["sources"]
                )
            ):
                raise CoreError("SOURCE_TYPE_UNSUPPORTED")
            jsonschema.Draft202012Validator(SCHEMAS[operation]).validate(params)
            self.budget.reserve(event, operation, params)
            if operation == "refresh_sources":
                result = await self.application.refresh_sources(
                    chat_request_id(event, operation),
                    status_only=params.get("action") == "status",
                    wait_seconds=params.get("wait_seconds", 10),
                )
            elif operation == "send_item_images":
                result = await self.application.send_item_images(
                    event, params, chat_request_id(event, operation)
                )
            elif operation in ("update_settings", "undo_settings"):
                semantic = {k: v for k, v in params.items() if k != "expected_version"}
                request_id = (
                    "settings-"
                    + hashlib.sha256(
                        json.dumps(
                            [chat_request_id(event, operation), semantic],
                            sort_keys=True,
                            ensure_ascii=False,
                        ).encode()
                    ).hexdigest()
                )
                result = await self.application.change_settings(operation, params, request_id)
            elif operation == "search_bilibili":
                result = await self.application.search_bilibili(
                    params, chat_request_id(event, operation)
                )
            else:
                result = await self.application.tool(operation, params)
        except (PluginError, CoreError) as exc:
            result = exc.as_dict()
        except (jsonschema.ValidationError, ValidationError, ValueError, TypeError):
            result = PluginError("INVALID_ARGUMENT").as_dict()
        except Exception as exc:
            log_failure(self.logger, "chat", key, exc)
            result = dict(PluginError("INTERNAL_ERROR").as_dict(), request_id=key)
        return json.dumps(public_guidance(result), ensure_ascii=False, separators=(",", ":"))

    async def _account_command(self, event, operation):
        event.should_call_llm(False)
        try:
            authorize_chat(event, self.context)
            result = await self.application.account_action(operation, event)
            if isinstance(result, dict):
                labels = {"valid": "已登录", "expired": "登录已失效", "not_logged_in": "尚未登录"}
                message = "B 站：" + labels.get(result["status"], "状态待确认")
                if result.get("uid"):
                    message += "\nUID：" + str(result["uid"])
                message += "\n自动失效提醒：" + ("开启" if result["auto_relogin"] else "关闭")
                if result["notification_bound"] and not result["proactive_supported"]:
                    message += "\n当前 QQ 适配器不支持主动二维码推送，可手动发送 /B站登录。"
                yield event.plain_result(message)
            else:
                yield event.plain_result(result)
        except PluginError as exc:
            yield event.plain_result(exc.message)
        except Exception as exc:
            log_failure(self.logger, "account_command", operation, exc)
            yield event.plain_result("B 站账号操作未完成，请稍后重试。")
        event.stop_event()

    @filter.command("B站登录")
    async def bili_login(self, event: AstrMessageEvent):
        async for result in self._account_command(event, "login"):
            yield result

    @filter.command("B站状态")
    async def bili_status(self, event: AstrMessageEvent):
        async for result in self._account_command(event, "status"):
            yield result

    @filter.command("B站退出")
    async def bili_logout(self, event: AstrMessageEvent):
        async for result in self._account_command(event, "logout"):
            yield result

    async def _body(self, operation):
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                raise PluginError("INVALID_ARGUMENT") from None
            if size > MAX_BODY_BYTES:
                raise PluginError("BODY_TOO_LARGE", status=413)
            if size <= 0:
                raise PluginError("INVALID_ARGUMENT")
        if (request.content_type or "").split(";", 1)[0].strip().lower() != "application/json":
            raise PluginError("INVALID_ARGUMENT", status=415)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise PluginError("BODY_TOO_LARGE", status=413)
        body = await request.json(default=None)
        if not isinstance(body, dict):
            raise PluginError("INVALID_ARGUMENT")
        key = body.get("key") if operation in ("feedback", "undo") else body.get("request_id")
        if not isinstance(key, str) or not IDENTIFIER.fullmatch(key):
            raise PluginError("REQUEST_REQUIRED")
        return body, key

    async def _web(self, operation, run_id=None):
        key = uuid.uuid4().hex
        try:
            authorize_web(request, self.context, self.settings)
            if list(request.query.keys()):
                raise PluginError("INVALID_ARGUMENT")
            params = {}
            if run_id is not None:
                if not IDENTIFIER.fullmatch(run_id):
                    raise PluginError("INVALID_ARGUMENT")
                params["run_id"] = run_id
            if operation not in ("state", "detail", "download"):
                body, key = await self._body(operation)
                if operation in ("collect", "generate"):
                    if set(body) != {"request_id"}:
                        raise PluginError("INVALID_ARGUMENT")
                    params["request_id"] = key
                else:
                    params["body"] = body
            result = await self.application.web(operation, params)
            if operation == "download":

                async def report():
                    yield result["report"].encode("utf-8")

                return stream_response(
                    report(),
                    content_type="text/markdown; charset=utf-8",
                    headers={
                        "Content-Disposition": f'attachment; filename="{result["filename"]}"',
                        "Cache-Control": "no-store",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            return json_response(result, headers={"Cache-Control": "no-store"})
        except PluginError as exc:
            return error_response(exc.message, status_code=exc.status, data=exc.as_dict())
        except CoreError as exc:
            status = (
                409
                if exc.code in ("SETTINGS_CONFLICT", "SETTINGS_BUSY", "IDEMPOTENCY_CONFLICT")
                else 400
            )
            return error_response(exc.message, status_code=status, data=exc.as_dict())
        except RequestError as exc:
            return error_response(str(exc), status_code=exc.status)
        except (ValidationError, ValueError, TypeError):
            error = PluginError("INVALID_ARGUMENT")
            return error_response(error.message, status_code=400, data=error.as_dict())
        except Exception as exc:
            log_failure(self.logger, operation, key, exc)
            error = dict(PluginError("INTERNAL_ERROR").as_dict(), request_id=key)
            return error_response(error["error_message"], status_code=500, data=error)

    async def web_state(self):
        return await self._web("state")

    async def web_detail(self, run_id: str):
        return await self._web("detail", run_id)

    async def web_download(self, run_id: str):
        return await self._web("download", run_id)

    async def web_collect(self):
        return await self._web("collect")

    async def web_generate(self):
        return await self._web("generate")

    async def web_profile(self):
        return await self._web("profile")

    async def web_feedback(self):
        return await self._web("feedback")

    async def web_undo(self):
        return await self._web("undo")

    def _unregister(self):
        owned = set(self._routes)
        routes = self.context.registered_web_apis
        routes[:] = [row for row in routes if (row[0], row[1], tuple(row[2])) not in owned]
        tools = self.context.get_llm_tool_manager().func_list
        tools[:] = [tool for tool in tools if not any(tool is item for item in self.tools)]

    async def terminate(self):
        if self._terminated:
            return
        self._terminated = True
        try:
            self._close_task = asyncio.create_task(self.application.close())
            await asyncio.shield(self._close_task)
        finally:
            self._unregister()
