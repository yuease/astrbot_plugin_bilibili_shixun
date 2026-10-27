"""Host-facing models. Tool inputs cannot select files, databases, goals or credentials."""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .text_evidence import clean_terms


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scoped(Contract):
    profile_id: str = Field(min_length=1, max_length=100)


class TimeRange(Contract):
    start: str | None = Field(None, max_length=64)
    end: str | None = Field(None, max_length=64)
    period: Literal["today", "last_3_days"] | None = None
    timezone: str = Field("Asia/Shanghai", max_length=80)

    @model_validator(mode="after")
    def exclusive(self):
        if self.period and (self.start or self.end):
            raise ValueError("period conflicts with bounds")
        return self


class SourceSelector(Contract):
    kind: Literal[
        "keyword_search",
        "up_uploads",
        "following_videos",
        "up_dynamics",
        "following_dynamics",
        "temporary_keyword",
    ]
    key: str | None = Field(None, max_length=100)


class SearchItemsRequest(Scoped):
    temporary_query_id: str | None = None
    has_images: bool | None = None
    terms: list[str] = Field(default_factory=list, max_length=8)
    _terms = field_validator("terms", mode="before")(clean_terms)
    time_range: TimeRange = Field(default_factory=TimeRange)
    sources: list[SourceSelector] = Field(default_factory=list, max_length=40)
    content_types: list[
        Literal["video", "text", "image", "article", "audio", "live", "unknown"]
    ] = Field(default_factory=list, max_length=7)
    limit: int = Field(20, ge=1, le=100)
    page: int = Field(1, ge=1, le=10000)
    collection_mode: Literal["existing_only", "temporary_keywords"] = "existing_only"
    temporary_keywords: list[str] = Field(default_factory=list, max_length=5)
    max_duration_seconds: int | None = Field(None, gt=0)
    timeout_seconds: float | None = Field(None, gt=0, le=120)
    request_id: str | None = Field(
        None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    snapshot_version: str | None = Field(None, max_length=64)


class GetItemRequest(Scoped):
    item_id: str = Field(min_length=1, max_length=150)
    max_body_chars: int = Field(4000, ge=1, le=20000)
    sources: list[SourceSelector] = Field(default_factory=list, max_length=40)
    timezone: str = Field("Asia/Shanghai", max_length=80)


class SourceStatusRequest(Scoped):
    include_disabled: bool = False
    timezone: str = Field("Asia/Shanghai", max_length=80)


class PreferencesRequest(Scoped):
    pass


class UpdatePreferencesRequest(Scoped):
    expected_version: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    add_keywords: list[str] = Field(default_factory=list, max_length=5)
    remove_keywords: list[str] = Field(default_factory=list, max_length=5)
    interests: str | None = Field(None, max_length=10000)
    interest_mode: Literal["same_goal", "switch_goal"] | None = None
    keyword_search_enabled: bool | None = None

    @model_validator(mode="after")
    def mode(self):
        if self.interests is not None and self.interest_mode is None:
            raise ValueError("interest_mode required")
        return self


class SubscriptionSwitchRequest(Scoped):
    expected_version: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    source: Literal["keyword_search", "up", "following"]
    enabled: bool = Field(strict=True)
    up_uid: str | None = Field(None, pattern=r"^[0-9]{1,30}$")
    following_scope: Literal["videos", "dynamics"] | None = None
    account_uid: str = ""  # Injected by the trusted host, never exposed as a tool argument.

    @model_validator(mode="after")
    def target(self):
        if (self.source == "up") != (self.up_uid is not None):
            raise ValueError("up_uid is required only for UP subscriptions")
        if self.following_scope is not None and self.source != "following":
            raise ValueError("following_scope is only valid for following subscriptions")
        return self


class UndoPreferenceChangeRequest(Scoped):
    change_id: str = Field(min_length=1, max_length=100)
    expected_version: str | None = None
    request_id: str | None = None


class SourceEvidence(Contract):
    kind: str
    key: str
    label: str
    collected_at: str | None = None
    published_at: str | None = None
    dynamic_id: str | None = None
    query_id: str | None = None
    temporary: bool = False
    url: str | None = None
    author: str | None = None
    author_uid: str | None = None


class TextSpan(Contract):
    id: str
    field: str
    start: int
    end: int
    text: str
    source: SourceEvidence


class BodySpan(Contract):
    field: str
    start: int
    end: int
    body_start: int
    body_end: int
    source: SourceEvidence


class Item(Contract):
    id: str
    title: str
    snippet: str
    author: str
    author_uid: str | None = None
    published_at: str | None = None
    matched_published_at: str | None = None
    time_basis: Literal["publication", "repost", "missing"] = "publication"
    activity_type: Literal["original", "repost"] = "original"
    activity_url: str | None = None
    forwarded_by: str | None = None
    original_published_at: str | None = None
    url: str
    content_type: str
    duration_seconds: int | None = None
    sources: list[SourceEvidence]
    dynamic_contexts: list[dict] = Field(default_factory=list)
    snippet_truncated: bool = False
    source_text_limited: bool = False
    sources_truncated: bool = False
    contexts_truncated: bool = False
    snippet_evidence: TextSpan | None = None


class ItemDetail(Item):
    body: str
    truncated: bool
    body_chars: int
    images: list[str] = Field(default_factory=list)
    images_analyzed: Literal[False] = False
    body_spans: list[BodySpan] = Field(default_factory=list)


class SourceStatus(Contract):
    kind: str
    key: str
    label: str
    availability: Literal["ready", "partial", "error", "unknown", "disabled"]
    last_checked_at: str | None = None
    last_success_at: str | None = None
    retained_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    temporary: bool = False
    scan: dict = Field(default_factory=dict)


class Coverage(Contract):
    scope: Literal["retained_allowed_data"] = "retained_allowed_data"
    timezone: str = "Asia/Shanghai"
    start: str | None = None
    end: str | None = None
    boundary: Literal["start_inclusive_end_exclusive"] = "start_inclusive_end_exclusive"
    as_of: str
    limited_sampling: Literal[True] = True
    insufficient: bool = True
    completeness: Literal["unknown", "incomplete"] = "unknown"
    note: str = "仅查询已采集并保留的内容，无法确认全部更新已覆盖。"
    reasons: list[str] = Field(default_factory=list)
    missing_publication_count: int = 0
    sources: list[SourceStatus] = Field(default_factory=list)
    sources_truncated: bool = False


class Usage(Contract):
    cache_hit: bool = False
    replayed: bool = False
    logical_model_calls: int = 0
    http_attempts: int | None = 0
    source_requests: int = 0
    actual_requests: int | None = 0
    elapsed_seconds: float = 0
    tokens: dict | None = None
    origin_metrics: dict | None = None

    @field_serializer("elapsed_seconds")
    def elapsed_precision(self, value):
        return round(value, 6)


class SearchResult(Contract):
    request_id: str
    outcome: Literal["found", "no_match", "collection_failed"]
    items: list[Item] = Field(default_factory=list)
    total_matches: int = 0
    page: int = 1
    has_more: bool = False
    truncated: bool = False
    retrieval_terms: list[str] = Field(default_factory=list)
    coverage: Coverage
    metrics: Usage = Field(default_factory=Usage)
    collection_failures: list[dict] = Field(default_factory=list)
    temporary_video_search_only: bool = False
    snapshot_version: str = ""
    next_page: int | None = None


class PreferenceView(Contract):
    profile_id: str
    version: str
    keywords: list[str]
    interests: str
    goal_id: str
    learning_context: str
    keyword_search_enabled: bool


class PreferenceChange(Contract):
    change_id: str
    before: PreferenceView
    after: PreferenceView
    changed_fields: list[str]
    needs_collection: bool = False
    data_ready: bool = False
    next_action: Literal["none", "refresh_collection", "enable_keyword_search"] = "none"
    promoted_items: int = 0
    replayed: bool = False
    undo_of: str | None = None


class SourceStatusResult(Contract):
    profile_id: str
    sources: list[SourceStatus]
    truncated: bool = False
    limited_sampling: Literal[True] = True
