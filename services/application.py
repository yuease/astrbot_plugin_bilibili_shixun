"""Own async tasks, domain threads, model access and plugin lifecycle."""

import asyncio
import hashlib
import json
import time

from ..core.agent_models import TimeRange
from ..core.briefing import local_now
from ..core.errors import RunFailure, classify, public_diagnostic
from ..core.source_types import SourceError
from .bilibili_login import BilibiliLogin
from .bilibili_source import BilibiliSource
from .errors import PluginError, log_failure
from .images import ImageDelivery
from .jobs import JobStore
from .lease import RuntimeLease
from .model import AstrBotModel
from .refresh import collection_summary, refresh_view
from .runtime import Runtime


class Application:
    def __init__(self, root, context, settings, logger):
        self.root, self.context, self.settings, self.logger = root, context, settings, logger
        self.runtime, self.jobs, self.lease = (
            Runtime(root, settings),
            JobStore(root),
            RuntimeLease(root),
        )
        self.lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.tasks = set()
        self.threads = set()
        self.scheduler = None
        self.source = self.model = self.login = None
        self.idle = asyncio.Event()
        self.idle.set()
        self.started = self.closing = False
        self.stop = asyncio.Event()
        self.schedule_signature = None
        self.next_due = None
        self.active = None
        self.job_tasks = {}
        self.images = ImageDelivery(context, self.io, logger, lambda: self.closing)

    async def io(self, function, *args, **kwargs):
        # Shield the thread from HTTP/tool cancellation and retain it until it
        # actually exits. Unload must not release the runtime while it writes.
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self.threads.add(task)

        def settled(done):
            self.threads.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(settled)
        return await asyncio.shield(task)

    async def start(self):
        if self.closing:
            raise PluginError("PLUGIN_CLOSED", status=503)
        async with self.start_lock:
            if self.closing:
                raise PluginError("PLUGIN_CLOSED", status=503)
            if self.started:
                return
            lease_task = asyncio.create_task(asyncio.to_thread(self.lease.acquire))
            try:
                await asyncio.shield(lease_task)
                await self.io(self.runtime.initialize)
                await self.io(self.jobs.initialize)
                loop = asyncio.get_running_loop()
                self.login = BilibiliLogin(
                    self.root, self.context, self.settings, self.logger, self.install_account
                )
                await self.login.start()
                await self.io(self.runtime.bind_account, self.login.public_state().get("uid") or "")
                self.source = BilibiliSource(loop, self.login, self.logger)
                self.model = AstrBotModel(self.context, self.settings, loop, self.logger)
                self.started = True
                self.scheduler = asyncio.create_task(self._schedule())
            except BaseException:
                await asyncio.gather(lease_task, return_exceptions=True)
                if self.login is not None:
                    await self.login.close()
                self.lease.close()
                raise

    async def tool(self, operation, params):
        await self.start()
        if operation == "get_settings":
            return await self.own(self._settings_read())
        return await self.own(self.io(self.runtime.tool, operation, params))

    async def _settings_read(self):
        async with self.lock:
            result = await self.io(self.runtime.get_settings)
            job = await self.io(self.jobs.latest, "collect", self.runtime.account_scope)
            result["collection_status"] = job["status"] if job else "not_started"
            active = [
                s
                for s in result["source_status"]
                if s.get("availability") != "disabled" and s.get("kind") != "temporary_keyword"
            ]
            result["data_status_scope"] = "enabled_long_term_sources"
            result["temporary_source_status"] = [
                s for s in result["source_status"] if s.get("kind") == "temporary_keyword"
            ]
            result["has_retained_results"] = any(s.get("retained_count", 0) for s in active)
            successful = bool(active) and all(s.get("last_success_at") for s in active)
            result["data_status"] = (
                "no_enabled_sources"
                if not active
                else (
                    (
                        "old_snapshot_after_failure"
                        if result["has_retained_results"]
                        else "collection_failed"
                    )
                    if any(s.get("availability") in {"partial", "error"} for s in active)
                    else "available"
                    if successful and result["has_retained_results"]
                    else "empty"
                    if successful
                    else "partial"
                    if result["has_retained_results"]
                    else "not_collected"
                )
            )
            return result

    async def change_settings(self, operation, params, request_id):
        await self.start()
        return await self.own(self._change_settings(operation, params, request_id))

    async def _change_settings(self, operation, params, request_id):
        async with self.lock:
            if self.closing:
                raise PluginError("PLUGIN_CLOSED")
            if self.active is not None or self.login.authenticating:
                raise PluginError("TASK_BUSY", status=409)
            function = (
                self.runtime.update_settings
                if operation == "update_settings"
                else self.runtime.undo_settings
            )
            return await self.io(function, params, request_id)

    async def search_bilibili(self, params, message_key):
        await self.start()
        return await self.own(self._search_bilibili(params, message_key))

    async def _search_bilibili(self, params, message_key):
        from datetime import datetime, timedelta

        action = params.get("action", "search")
        wait = params.get("wait_seconds", 10)
        if type(wait) is not int or not 0 <= wait <= 20:
            raise PluginError("INVALID_ARGUMENT")
        if action not in {"search", "status"}:
            raise PluginError("INVALID_ARGUMENT")
        async with self.lock:
            scope = self.runtime.account_scope
            if action == "status":
                if set(params) - {"action", "search_id", "wait_seconds"} or not params.get(
                    "search_id"
                ):
                    raise PluginError("INVALID_ARGUMENT")
                job = await self.io(self.jobs.get, "temporary_search", params["search_id"], scope)
                if job is None:
                    raise PluginError("NOT_FOUND")
            else:
                if (
                    not isinstance(params.get("keyword"), str)
                    or not params["keyword"].strip()
                    or len(params["keyword"]) > 100
                    or "search_id" in params
                ):
                    raise PluginError("INVALID_ARGUMENT")
                if (
                    type(params.get("remote_page", 1)) is not int
                    or not 1 <= params.get("remote_page", 1) <= 3
                    or type(params.get("limit", 5)) is not int
                    or not 1 <= params.get("limit", 5) <= 10
                ):
                    raise PluginError("INVALID_ARGUMENT")
                payload = {
                    "keyword": params["keyword"].strip(),
                    "remote_page": params.get("remote_page", 1),
                    "limit": params.get("limit", 5),
                    "time_range": TimeRange.model_validate(
                        params.get("time_range", {})
                    ).model_dump(),
                }
                from ..core.retrieval import resolve_range

                resolve_range(TimeRange.model_validate(payload["time_range"]), "", local_now())
                request_id = (
                    "tmp-"
                    + hashlib.sha256(
                        json.dumps(
                            [message_key, payload], sort_keys=True, ensure_ascii=False
                        ).encode()
                    ).hexdigest()
                )
                existing = await self.io(self.jobs.get, "temporary_search", request_id, scope)
                if self.active is not None and existing is None:
                    raise PluginError("TASK_BUSY", status=409)
                job, created = await self.io(
                    self.jobs.start,
                    "temporary_search",
                    request_id,
                    scope,
                    payload=payload,
                    message_key=message_key,
                )
                if created:
                    self.active = job["id"]
                    self.idle.clear()
                    task = asyncio.create_task(self._run_temporary(job))
                    self.job_tasks[job["id"]] = task
                    self.tasks.add(task)
                    task.add_done_callback(self._job_done)
                    task.add_done_callback(
                        lambda done, key=job["id"]: self.job_tasks.pop(key, None)
                    )
        timed_out = False
        task = self.job_tasks.get(job["id"])
        if job["status"] == "running" and task is not None and wait:
            try:
                await asyncio.wait_for(asyncio.shield(task), wait)
            except TimeoutError:
                timed_out = True
        async with self.lock:
            if self.runtime.account_scope != scope:
                raise PluginError("REQUEST_CONFLICT", status=409)
            job = await self.io(self.jobs.get, "temporary_search", job["id"], scope)
        expires = datetime.fromisoformat(job["started"]) + timedelta(hours=24)
        if local_now() >= expires:
            raise PluginError("TEMP_SEARCH_EXPIRED")
        result = {
            k: job[k]
            for k in ("id", "status", "started", "finished", "error_code", "error_message")
            if k in job
        }
        result.update(
            operation_finished=job["status"] in {"completed", "failed"},
            subscription_saved=False,
            collection_status=job["status"],
            results_available=bool(job.get("result", {}).get("items")),
            wait_timed_out=timed_out and job["status"] == "running",
            expires_at=expires.isoformat(),
        )
        if job["status"] == "completed":
            result["result"] = job["result"]
        elif job["status"] == "failed":
            result["guidance"] = (
                "匿名搜索已失败并结束；这不是无匹配。不要再用status等待，不自动重试或改词；"
                "仅在用户要求重试时重新search，不要用shell绕过。"
            )
        else:
            result["guidance"] = (
                "匿名搜索仍在运行，不能当作无匹配；可用action=status、search_id=id继续有限等待，不要用shell重试。"
            )
        return result

    async def _run_temporary(self, job):
        job = dict(job)
        try:
            response = await self.source.search_public(
                job["params"]["keyword"], job["params"]["remote_page"], timeout=20
            )
            result = await self.io(self.runtime.temporary_result, job, response)
            job.update(status="completed", result=result, record_available=True)
        except Exception as exc:
            log_failure(self.logger, "temporary_search", "anonymous", exc)
            error = exc.diagnosis if isinstance(exc, SourceError) else classify(exc, "collect")
            job.update(status="failed", **public_diagnostic(error))
            try:
                await self.io(self.runtime.temporary_failure, job, job["error_code"])
            except Exception as storage_error:
                log_failure(self.logger, "temporary_failure_record", "anonymous", storage_error)
        finally:
            job["finished"] = local_now().isoformat()
            try:
                await self.io(self.jobs.finish, job)
            finally:
                self.active = None
                self.idle.set()

    async def send_item_images(self, event, params, request_id):
        await self.start()
        return await self.own(self._send_item_images(event, params, request_id))

    async def _send_item_images(self, event, params, request_id):
        async with self.lock:
            if self.closing:
                raise PluginError("PLUGIN_CLOSED")
            indices = params.get("indices", [0])
            key = hashlib.sha256(
                json.dumps(
                    [
                        request_id,
                        self.runtime.account_scope,
                        params["item_id"],
                        sorted(indices),
                        params.get("quality", "original"),
                    ]
                ).encode()
            ).hexdigest()
            item = await self.io(
                self.runtime.tool, "get_item", {"item_id": params["item_id"], "max_body_chars": 1}
            )
            return await self.images.send(
                event, item, indices, key, params.get("quality", "original")
            )

    async def refresh_sources(self, request_id, *, status_only=False, wait_seconds=0):
        if type(wait_seconds) is not int or not 0 <= wait_seconds <= 20:
            raise PluginError("INVALID_ARGUMENT")
        await self.start()
        return await self.own(self._refresh_sources(request_id, status_only, wait_seconds))

    async def _refresh_sources(self, request_id, status_only, wait_seconds):
        if status_only:
            async with self.lock:
                job = await self.io(self.jobs.latest, "collect", self.runtime.account_scope)
        else:
            job = await self.start_job("collect", request_id, reuse_collection=True)
        if job is None:
            return {
                "status": "not_started",
                "operation_finished": False,
                "operation_succeeded": False,
                "data_complete": None,
                "warnings": [],
                "guidance": "当前账号尚无采集任务；需要刷新时使用action=start。",
            }
        scope = job["account_scope"]
        started = time.monotonic()
        timed_out = False
        task = self.job_tasks.get(job["id"])
        if job["status"] == "running" and task is not None and wait_seconds:
            try:
                await asyncio.wait_for(asyncio.shield(task), wait_seconds)
            except TimeoutError:
                timed_out = True
        async with self.lock:
            if scope != self.runtime.account_scope:
                raise PluginError("REQUEST_CONFLICT", status=409)
            latest = await self.io(self.jobs.get, "collect", job["id"], scope)
        if latest is not None:
            latest = dict(latest, **({"reused_running": True} if job.get("reused_running") else {}))
            job = latest
        result = refresh_view(job)
        result.update(
            waited_seconds=round(time.monotonic() - started, 3),
            wait_timed_out=timed_out and job["status"] == "running",
        )
        return result

    async def own(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self._job_done)
        return await asyncio.shield(task)

    async def state(self):
        data = await self.io(self.runtime.state)
        scope = data["profile"]["account_scope"]
        data["job"] = await self.io(self.jobs.latest, "generate", scope)
        data["collection_job"] = await self.io(self.jobs.latest, "collect", scope)
        temporary = await self.io(self.jobs.latest, "temporary_search", scope)
        data["temporary_search_job"] = (
            {
                k: temporary[k]
                for k in (
                    "id",
                    "status",
                    "started",
                    "finished",
                    "error_code",
                    "error_message",
                    "results_expired",
                )
                if k in temporary
            }
            if temporary
            else None
        )
        if temporary:
            data["temporary_search_job"].update(
                keyword=temporary.get("params", {}).get("keyword"),
                result_count=len(temporary.get("result", {}).get("items", [])),
            )
        data["model"] = {
            "provider_id": self.settings.briefing_provider_id or None,
            "uses_astrbot_default": not self.settings.briefing_provider_id,
        }
        data["bilibili_account"] = self.login.public_state()
        return data

    async def install_account(self, uid):
        while not self.closing:
            await self.idle.wait()
            async with self.lock:
                if self.active is None:
                    await self.io(self.runtime.bind_account, uid)
                    return
        raise PluginError("PLUGIN_CLOSED")

    async def account_action(self, operation, event):
        await self.start()
        if operation == "status":
            return self.login.public_state()
        async with self.lock:
            if self.active is not None:
                raise PluginError("TASK_BUSY", status=409)
        if operation == "login":
            return await self.own(self.login.request_login(event))
        if operation == "logout":
            await self.own(self.login.logout(event))
            return "已清除插件保存的 B 站登录凭据和通知绑定，不再自动发送二维码。"
        raise PluginError("INVALID_ARGUMENT")

    async def web(self, operation, params):
        await self.start()
        if operation == "state":
            return await self.own(self.state())
        if operation in ("detail", "download"):
            return await self.own(
                self.io(self.runtime.detail, params["run_id"], operation == "download")
            )
        return await self.own(self._write(operation, params))

    async def _write(self, operation, params):
        if operation in ("collect", "generate"):
            return await self.start_job(operation, params["request_id"])
        async with self.lock:
            if self.closing:
                raise PluginError("PLUGIN_CLOSED", status=503)
            if operation == "profile":
                if self.active is not None:
                    raise PluginError("TASK_BUSY", status=409)
                return await self.io(self.runtime.save_profile, params["body"])
            return await self.io(self.runtime.feedback, params["body"], operation == "undo")

    async def start_job(self, operation, request_id, *, reuse_collection=False):
        async with self.lock:
            if self.closing:
                raise PluginError("PLUGIN_CLOSED", status=503)
            profile = await self.io(self.runtime.require_profile)
            from ..core.sources import subscriptions

            if not subscriptions(profile):
                raise PluginError("NOT_CONFIGURED")
            if reuse_collection:
                previous = await self.io(
                    self.jobs.get, operation, request_id, self.runtime.account_scope
                )
                if previous is not None:
                    return previous
            if self.login.authenticating:
                raise PluginError("BILI_LOGIN_BUSY")
            requires_login = any(ref["kind"] != "keyword_search" for ref in subscriptions(profile))
            if operation == "collect" and requires_login and not self.login.uid:
                raise PluginError("BILI_LOGIN_REQUIRED")
            if reuse_collection and self.active is not None:
                existing = await self.io(self.jobs.latest, "collect", self.runtime.account_scope)
                if existing and existing["id"] == self.active and existing["status"] == "running":
                    return await self.io(
                        self.jobs.reuse_collection,
                        request_id,
                        existing["id"],
                        self.runtime.account_scope,
                    )
                raise PluginError("TASK_BUSY", status=409)
            job, created = await self.io(
                self.jobs.start, operation, request_id, self.runtime.account_scope
            )
            if created:
                self.active = job["id"]
                self.idle.clear()
                task = asyncio.create_task(self._run(job))
                self.job_tasks[job["id"]] = task
                task.add_done_callback(lambda done, key=job["id"]: self.job_tasks.pop(key, None))
                self.tasks.add(task)
                task.add_done_callback(self._job_done)
            return job

    def _job_done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log_failure(self.logger, "job_lifecycle", "background", task.exception())

    async def _run(self, job):
        job = dict(job)
        try:
            result = await self.io(
                self.runtime.run_job, job["operation"], job["run_id"], self.source, self.model
            )
            job.update(status="completed", record_available=True)
            if job["operation"] == "collect":
                job["collection_summary"] = collection_summary(result)
            if result.get("failures"):
                job["warning"] = "部分来源采集失败，已保存可用结果。"
        except Exception as exc:
            log_failure(self.logger, job["operation"], job["id"], exc)
            error = exc.diagnosis if isinstance(exc, RunFailure) else classify(exc, "unknown")
            job.update(status="failed", **public_diagnostic(error))
            try:
                detail = await self.io(self.runtime.detail, job["run_id"])
                job["record_available"] = True
                if detail["status"] in ("completed", "empty", "collection"):
                    job.update(status="completed", warning=error.get("error_message"))
            except Exception:
                job["record_available"] = False
        finally:
            job["finished"] = local_now().isoformat()
            try:
                await self.io(self.jobs.finish, job)
            finally:
                self.active = None
                self.idle.set()

    async def _schedule(self):
        while not self.stop.is_set():
            try:
                async with self.lock:
                    if self.runtime.path.exists() and not self.login.authenticating:
                        profile = await self.io(self.runtime.require_profile)
                        from ..core.sources import subscriptions

                        enabled_refs = subscriptions(profile)
                        signature = (
                            profile.collection.enabled,
                            profile.collection.interval_minutes,
                        )
                        if signature != self.schedule_signature:
                            self.schedule_signature = signature
                            self.next_due = (
                                time.monotonic() + profile.collection.interval_minutes * 60
                            )
                        due = (
                            signature[0]
                            and self.next_due <= time.monotonic()
                            and self.active is None
                            and bool(enabled_refs)
                            and (
                                bool(self.login.uid)
                                or all(r["kind"] == "keyword_search" for r in enabled_refs)
                            )
                        )
                    else:
                        due = False
                if due and not self.closing:
                    import uuid

                    await self.start_job("collect", "scheduled-" + uuid.uuid4().hex)
                    self.next_due = time.monotonic() + profile.collection.interval_minutes * 60
            except Exception as exc:
                log_failure(self.logger, "scheduler", "scheduled", exc)
            try:
                await asyncio.wait_for(self.stop.wait(), 5)
            except TimeoutError:
                pass

    async def close(self):
        self.closing = True
        self.stop.set()
        async with self.start_lock:
            if self.scheduler is not None:
                await self.scheduler
            if self.login is not None:
                await self.login.close()
            # No independent process can be killed. Let bounded Bilibili/model calls
            # settle before releasing the runtime or its async HTTP transport.
            while self.tasks:
                await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
            while self.threads:
                await asyncio.gather(*tuple(self.threads), return_exceptions=True)
            self.lease.close()
            self.started = False
