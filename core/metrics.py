"""Count SDK HTTP attempts separately from logical selection calls."""

from contextvars import ContextVar

attempts = ContextVar("bilibili_shixun_http_attempts", default=None)


def record_request(request):
    counter = attempts.get()
    if counter is not None:
        counter["http_attempts"] += 1
        if counter.get("on_request"):
            try:
                counter["on_request"](counter)
            except Exception:
                pass
