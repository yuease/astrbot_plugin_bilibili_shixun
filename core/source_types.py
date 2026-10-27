"""Transport-independent collection batches and safe source errors."""

from .errors import diagnostic


class SourceError(RuntimeError):
    def __init__(self, code="SOURCE_UNKNOWN", *, status="failed", details=None):
        self.status = status
        self.diagnosis = diagnostic(code, details)
        super().__init__(self.diagnosis["error_message"])


class SourceBatch(list):
    def __init__(self, items, *, raw_ids=(), cursor=None, has_more=False, times=()):
        super().__init__(items)
        self.raw_ids = list(raw_ids)
        self.cursor = str(cursor) if cursor is not None else None
        self.has_more = bool(has_more)
        stamps = list(times)
        self.newest = max(stamps) if stamps and len(stamps) == len(self.raw_ids) else None
        self.oldest = min(stamps) if stamps and len(stamps) == len(self.raw_ids) else None
