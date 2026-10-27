"""Typed read-only Bilibili acquisition through the maintained async SDK."""

import asyncio
import time

from ..core.attachments import poll_attachment
from ..core.bilibili_items import dynamic_batch, video_batch
from ..core.source_types import SourceError
from .errors import PluginError, log_failure


def search_results(data):
    """Accept the observed zero-hit envelope, never arbitrary missing payloads."""
    if not isinstance(data, dict):
        raise SourceError("SOURCE_PARSE")
    if any(data.get(key) not in (None, 0, False) for key in ("code", "in_black_key")):
        raise SourceError("SOURCE_PARSE")
    if "result" in data:
        if not isinstance(data["result"], list):
            raise SourceError("SOURCE_PARSE")
        return data["result"]
    if (
        type(data.get("numResults")) is int
        and data["numResults"] == 0
        and type(data.get("numPages")) is int
        and data["numPages"] == 0
        and data.get("in_black_key") == 0
    ):
        return []
    raise SourceError("SOURCE_PARSE")


class BilibiliSource:
    def __init__(self, loop, login, logger):
        self.loop, self.login, self.logger = loop, login, logger
        self.lock = asyncio.Lock()
        self.last_request = 0.0
        self.vote_cache = {}

    async def _complete_polls(self, batch, credential, deadline):
        from bilibili_api import Api
        from bilibili_api.utils.utils import get_api

        requests = 0
        for article in batch:
            for attachment in article.attachments:
                if (
                    attachment.get("type") != "poll"
                    or attachment.get("details_status") == "complete"
                    or not attachment.get("id")
                ):
                    continue
                identity = attachment["id"]
                cache_key = (self.login.uid, identity)
                cached = self.vote_cache.get(cache_key)
                if cached and cached[0] > time.monotonic():
                    attachment.update(cached[1])
                    continue
                remaining = deadline - time.monotonic() - 1
                if requests >= 2 or remaining <= 0.1:
                    continue
                requests += 1
                try:
                    async with asyncio.timeout(min(3.0, remaining)):
                        await asyncio.sleep(max(0, 1.0 - (time.monotonic() - self.last_request)))
                        self.last_request = time.monotonic()
                        response = (
                            await Api(**get_api("vote")["info"]["vote_info"], credential=credential)
                            .update_params(vote_id=int(identity))
                            .result
                        )
                        info = response.get("info")
                        if not isinstance(info, dict):
                            raise ValueError("invalid poll response")
                        parsed = poll_attachment(dict(info, vote_id=identity))
                        attachment.update(parsed)
                        self.vote_cache[cache_key] = (time.monotonic() + 60, dict(parsed))
                        while len(self.vote_cache) > 64:
                            self.vote_cache.pop(next(iter(self.vote_cache)))
                except Exception as exc:
                    log_failure(self.logger, "poll_attachment", "read", exc)
                    attachment["details_status"] = "unavailable"
            if (
                article.attachments
                and article.images_status == "known"
                and all(a.get("details_status") == "complete" for a in article.attachments)
            ):
                article.attachments_status = "known"
        return batch

    def fetch(self, ref, *, account_scope, cursor=None, timeout=20):
        return asyncio.run_coroutine_threadsafe(
            self._fetch(ref, account_scope, cursor, timeout), self.loop
        ).result()

    async def search_public(self, keyword, remote_page=1, timeout=20):
        from bilibili_api import Api, Credential, request_log
        from bilibili_api.utils.utils import get_api

        try:
            async with asyncio.timeout(timeout):
                async with self.lock:
                    if request_log.is_on():
                        raise SourceError("SOURCE_DEBUG_LOGGING")
                    await asyncio.sleep(max(0, 1.0 - (time.monotonic() - self.last_request)))
                    self.last_request = time.monotonic()
                    data = (
                        await Api(
                            **get_api("search")["search"]["web_search_by_type"],
                            wbi=True,
                            credential=Credential(),
                        )
                        .update_params(
                            keyword=keyword,
                            search_type="video",
                            order="pubdate",
                            duration=0,
                            page=remote_page,
                            page_size=20,
                        )
                        .result
                    )
                    values = search_results(data)
                    pages, total = data.get("numPages"), data.get("numResults")
                    return {
                        "items": video_batch(values),
                        "remote_has_more": remote_page < pages if type(pages) is int else None,
                        "reported_total": total if type(total) is int else None,
                    }
        except SourceError:
            raise
        except TimeoutError:
            raise SourceError("SOURCE_TIMEOUT") from None
        except Exception as exc:
            log_failure(self.logger, "public_search", "anonymous", exc)
            raise SourceError(
                "SOURCE_RATE_LIMIT"
                if getattr(exc, "code", None) in (-352, -412, -509)
                else "SOURCE_HTTP"
            ) from None

    async def _fetch(self, ref, account_scope, cursor, timeout):
        from bilibili_api import Api, Credential, dynamic, request_log, user
        from bilibili_api.utils.utils import get_api

        revision = self.login.revision
        deadline = time.monotonic() + timeout
        try:
            async with asyncio.timeout(timeout):
                async with self.lock:
                    public_keyword = ref["kind"] == "keyword_search"
                    if not public_keyword and not self.login.uid:
                        status = (
                            "auth_expired"
                            if self.login.public_state()["status"] == "expired"
                            else "auth_required"
                        )
                        raise SourceError(
                            "SOURCE_AUTH_EXPIRED"
                            if status == "auth_expired"
                            else "SOURCE_AUTH_REQUIRED",
                            status=status,
                        )
                    if not public_keyword and self.login.uid != account_scope:
                        raise SourceError("SOURCE_ACCOUNT_CHANGED")
                    if request_log.is_on():
                        # The SDK's optional request logger includes cookies.
                        # Refuse such a call without mutating another plugin's logger.
                        raise SourceError("SOURCE_DEBUG_LOGGING")
                    credential = Credential() if public_keyword else self.login.credentials()
                    await asyncio.sleep(max(0, 1.0 - (time.monotonic() - self.last_request)))
                    self.last_request = time.monotonic()
                    kind = ref["kind"]
                    if kind == "keyword_search":
                        api = get_api("search")["search"]["web_search_by_type"]
                        data = (
                            await Api(**api, wbi=True, credential=credential)
                            .update_params(
                                keyword=ref["key"],
                                search_type="video",
                                order="pubdate",
                                duration=0,
                                page=1,
                                page_size=30,
                            )
                            .result
                        )
                        values = search_results(data)
                        return video_batch(values)
                    if kind == "up_uploads":
                        data = await user.User(
                            uid=int(ref["key"]), credential=credential
                        ).get_videos(pn=1, ps=30, order=user.VideoOrder.PUBDATE)
                        values = data.get("list", {}).get("vlist", [])
                        if not isinstance(values, list):
                            raise SourceError("SOURCE_PARSE")
                        return video_batch(values)
                    if kind == "up_dynamics":
                        data = await user.User(
                            uid=int(ref["key"]), credential=credential
                        ).get_dynamics_new(offset=cursor or "")
                        return await self._complete_polls(dynamic_batch(data), credential, deadline)
                    if kind in ("following_videos", "following_dynamics"):
                        if ref["key"] != self.login.uid:
                            raise SourceError("SOURCE_ACCOUNT_MISMATCH")
                        data = await dynamic.get_dynamic_page_info(
                            credential=credential,
                            _type=dynamic.DynamicType.VIDEO
                            if kind == "following_videos"
                            else dynamic.DynamicType.ALL,
                            offset=cursor,
                            pn=1,
                        )
                        return await self._complete_polls(
                            dynamic_batch(data, videos_only=kind == "following_videos"),
                            credential,
                            deadline,
                        )
                    raise SourceError("SOURCE_PARSE")
        except SourceError:
            raise
        except PluginError:
            raise SourceError("SOURCE_AUTH_REQUIRED", status="auth_required") from None
        except (TimeoutError, ConnectionError):
            raise SourceError("SOURCE_TIMEOUT") from None
        except Exception as exc:
            log_failure(self.logger, "bilibili_source", ref["kind"], exc)
            code = getattr(exc, "code", None)
            if code == -101 and ref["kind"] != "keyword_search":
                await self.login.mark_expired(revision)
                raise SourceError("SOURCE_AUTH_EXPIRED", status="auth_expired") from None
            if code in (-352, -412, -509):
                raise SourceError("SOURCE_RATE_LIMIT") from None
            raise SourceError("SOURCE_HTTP" if type(code) is int else "SOURCE_UNKNOWN") from None
