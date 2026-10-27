"""Single logical briefing call through AstrBot; no independent model SDK."""

import asyncio
import json
import re

from ..core.errors import DiagnosedError
from .errors import log_failure


class AstrBotModel:
    def __init__(self, context, settings, loop, logger, *, gate=None, umo=None):
        self.context, self.settings, self.loop, self.logger = context, settings, loop, logger
        self.model_name = settings.briefing_provider_id or "AstrBot default"
        self.usage = None
        self.gate, self.umo = gate, umo

    def select(self, prompt, schema):
        """Called only by a domain job thread, never the host event loop."""
        future = asyncio.run_coroutine_threadsafe(self._select(prompt, schema), self.loop)
        # _select owns the timeout. Do not start another attempt on a wait failure.
        return future.result()

    async def _select(self, prompt, schema):
        if self.gate is not None:
            async with self.gate:
                return await self._select_owned(prompt, schema)
        return await self._select_owned(prompt, schema)

    async def _select_owned(self, prompt, schema):
        self.usage = None
        provider_id = self.settings.briefing_provider_id
        if not provider_id:
            provider = (
                await self.context.get_using_provider_async(self.umo)
                if self.umo
                else await self.context.get_using_provider_async()
            )
            if provider is None:
                raise DiagnosedError("MODEL_CONFIG_MISSING")
            provider_id = provider.meta().id
        self.model_name = provider_id
        instructions = (
            "只输出一个合法 JSON 对象，不输出 Markdown、解释或思维过程。"
            '必须含 picks 数组；没有推荐时输出 {"picks":[]}。遵守以下 JSON Schema：\n'
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=instructions,
                    max_tokens=4000,
                ),
                self.settings.model_timeout_seconds,
            )
        except Exception as exc:
            log_failure(self.logger, "briefing_model", "provider-call", exc)
            if isinstance(exc, TimeoutError):
                raise DiagnosedError("MODEL_TIMEOUT") from None
            status = getattr(exc, "status_code", None)
            code = {401: "MODEL_AUTH", 403: "MODEL_PERMISSION", 429: "MODEL_RATE_LIMIT"}.get(status)
            raise DiagnosedError(code or "MODEL_CONNECTION") from None
        text = response.completion_text
        if not isinstance(text, str) or len(text) > 100_000:
            raise DiagnosedError("MODEL_PARSE")
        text = text.strip()
        fence = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
        if fence:
            text = fence.group(1)
        try:
            value = json.loads(text)
        except ValueError:
            raise DiagnosedError("MODEL_PARSE") from None
        if (
            not isinstance(value, dict)
            or set(value) != {"picks"}
            or not isinstance(value["picks"], list)
        ):
            raise DiagnosedError("MODEL_FIELDS")
        usage = getattr(response, "usage", None)
        self.usage = (
            {
                "input_tokens": usage.input,
                "output_tokens": usage.output,
                "total_tokens": usage.total,
            }
            if usage is not None
            else None
        )
        return schema.model_validate(value)
