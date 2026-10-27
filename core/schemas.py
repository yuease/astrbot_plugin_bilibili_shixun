"""Shared contracts; legacy profiles and Python entry points remain supported."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def uid_string(value):
    if (
        not isinstance(value, str)
        or not value.strip().isascii()
        or not value.strip().isdigit()
        or not 1 <= len(value.strip()) <= 30
    ):
        raise ValueError("UID must be a 1–30 digit string")
    return value.strip()


class KeywordSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True


class UpSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    uids: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("uids", mode="before")
    @classmethod
    def clean_uids(cls, values):
        if not isinstance(values, list):
            raise ValueError("uids must be a list")
        return list(dict.fromkeys(uid_string(v) for v in values))


class FollowingSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    uid: str = ""

    @field_validator("uid", mode="before")
    @classmethod
    def clean_uid(cls, value):
        return "" if isinstance(value, str) and not value.strip() else uid_string(value)


class Sources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword_search: KeywordSource = Field(default_factory=KeywordSource)
    up_uploads: UpSource = Field(default_factory=UpSource)
    following_videos: FollowingSource = Field(default_factory=FollowingSource)


class UpSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uid: str
    enabled: bool = True
    scope: Literal["uploads", "dynamics", "both"] = "uploads"
    content_type: Literal["all", "video", "non_video"] = "all"
    _uid = field_validator("uid", mode="before")(uid_string)


class FollowingSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uid: str = ""
    enabled: bool = False
    scope: Literal["videos", "dynamics"] = "videos"
    content_type: Literal["all", "video", "non_video"] = "all"

    @field_validator("uid", mode="before")
    @classmethod
    def valid_uid(cls, value):
        return "" if value == "" else uid_string(value)


class SourcePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keyword_search: KeywordSource = Field(default_factory=KeywordSource)
    ups: list[UpSubscription] = Field(default_factory=list, max_length=10)
    following: FollowingSubscription = Field(default_factory=FollowingSubscription)

    @model_validator(mode="after")
    def unique(self):
        if len({u.uid for u in self.ups}) != len(self.ups):
            raise ValueError("duplicate UP UID")
        return self


class RuleSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version: int = Field(1, ge=1)
    enabled: bool = True


class BlockedUp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    uid: str
    scope: Literal["dynamics", "all"] = "dynamics"
    _uid = field_validator("uid", mode="before")(uid_string)


class Filters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[RuleSetting] = Field(
        default_factory=lambda: [RuleSetting(id="lottery"), RuleSetting(id="advertisement")],
        max_length=50,
    )
    blocked_ups: list[BlockedUp] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def unique(self):
        if len({r.id for r in self.rules}) != len(self.rules) or len(
            {r.uid for r in self.blocked_ups}
        ) != len(self.blocked_ups):
            raise ValueError("duplicate filter ID or UID")
        return self


class Collection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    interval_minutes: Literal[30, 60, 120, 360] = 60
    dynamic_ttl_hours: Literal[24, 72, 168] = 72
    dynamic_batches: int = Field(3, ge=1, le=3)
    max_requests: int = Field(48, ge=1, le=96)
    max_seconds: float = Field(120, gt=0, le=600)
    future_tolerance_seconds: int = Field(300, ge=0, le=3600)


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "keyword_search",
        "up_uploads",
        "following_videos",
        "up_dynamics",
        "following_dynamics",
        "temporary_keyword",
    ]
    key: str
    label: str


class Learning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    half_life_days: float = Field(60, gt=0)
    max_score_adjustment: float = Field(5, ge=0, le=5)
    max_rank_movement: int = Field(1, ge=0, le=1)
    activation_min_videos: int = Field(3, ge=2)
    activation_min_days: int = Field(3, ge=2)
    activation_net: float = Field(1.5, gt=0)
    activation_consistency: float = Field(0.7, gt=0.5, le=1)
    daily_cap: float = Field(1, gt=0, le=1)
    max_tags: int = Field(5, ge=1, le=20)
    duration_buckets: list[int] = Field(default_factory=lambda: [300, 1200, 3600])

    @field_validator("duration_buckets")
    @classmethod
    def buckets(cls, v):
        if v != sorted(set(v)) or any(x <= 0 for x in v):
            raise ValueError("duration_buckets must be positive and increasing")
        return v


class InterestIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    goal_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    legacy_anchor: str | None = None


class RetrievalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_output_chars: int = Field(16000, ge=2000, le=50000)
    max_snippet_chars: int = Field(800, ge=100, le=2000)
    max_body_chars: int = Field(8000, ge=100, le=20000)
    aliases: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("aliases")
    @classmethod
    def bounded_aliases(cls, value):
        if len(value) > 100 or any(
            not k.strip()
            or len(k) > 100
            or len(v) > 10
            or any(not t.strip() or len(t) > 100 for t in v)
            for k, v in value.items()
        ):
            raise ValueError("invalid aliases")
        return value


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(4, ge=1, le=4)
    profile_id: str = Field("default", min_length=1, max_length=100)
    account_scope: str = ""
    keywords: list[str] = Field(default_factory=list, max_length=5)
    sources: Sources = Field(default_factory=Sources)
    source_plan: SourcePlan | None = None
    filters: Filters = Field(default_factory=Filters)
    collection: Collection = Field(default_factory=Collection)
    interests: str
    interest_identity: InterestIdentity | None = None
    retrieval: RetrievalPolicy = Field(default_factory=RetrievalPolicy)
    max_picks: int = Field(5, ge=1, le=10)
    max_candidates: int = Field(60, ge=1, le=200)
    max_duration_seconds: int | None = Field(None, gt=0)
    exploration_ratio: float = Field(0.2, ge=0, le=1)
    history_cooldown_days: float = Field(14, ge=0)
    snooze_days: float = Field(3, ge=0)
    learning: Learning = Field(default_factory=Learning)
    source_timeout_seconds: float = Field(30, gt=0, le=90)

    @model_validator(mode="before")
    @classmethod
    def legacy_sources(cls, value):
        if isinstance(value, dict) and value.get("source_plan") is None:
            value = dict(value)
            old = Sources.model_validate(value.get("sources", {}))
            value["source_plan"] = dict(
                keyword_search=old.keyword_search.model_dump(),
                ups=[
                    dict(uid=u, enabled=old.up_uploads.enabled, scope="uploads")
                    for u in old.up_uploads.uids
                ],
                following=dict(
                    uid=old.following_videos.uid,
                    enabled=old.following_videos.enabled,
                    scope="videos",
                ),
            )
        return value

    @model_validator(mode="after")
    def enabled_sources(self):
        s = self.source_plan
        if s is None:
            raise ValueError("source plan required")
        if s.keyword_search.enabled and not self.keywords:
            raise ValueError("enabled keyword search requires keywords")
        if s.following.enabled and not s.following.uid:
            raise ValueError("enabled following source requires UID")
        # Compatibility projection only. source_plan is the single authority when present.
        uploads = [u for u in s.ups if u.scope in ("uploads", "both")]
        self.sources = Sources(
            keyword_search=s.keyword_search,
            up_uploads=UpSource(
                enabled=any(u.enabled for u in uploads), uids=[u.uid for u in uploads]
            ),
            following_videos=FollowingSource(
                uid=s.following.uid, enabled=s.following.enabled and s.following.scope == "videos"
            ),
        )
        return self

    @field_validator("account_scope")
    @classmethod
    def account(cls, value):
        return "" if value == "" else uid_string(value)

    @field_validator("keywords")
    @classmethod
    def keywords_valid(cls, v):
        v = list(dict.fromkeys(x.strip() for x in v))
        if not all(v):
            raise ValueError("empty keyword")
        return v

    @property
    def context(self):
        # Stable goal + feature interpretation; acquisition and temporary limits are independent.
        identity = self.interest_identity
        if identity and identity.legacy_anchor is None:
            value = [
                self.account_scope,
                "stable-v2",
                self.profile_id,
                identity.goal_id,
                self.learning.duration_buckets,
                self.learning.max_tags,
            ]
        else:
            anchor = identity.legacy_anchor if identity else self.interests.strip()
            value = [
                self.account_scope,
                "stable-v1",
                self.profile_id,
                anchor,
                self.learning.duration_buckets,
                self.learning.max_tags,
            ]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()

    @property
    def legacy_context(self):
        value = [
            self.account_scope,
            self.profile_id,
            sorted(self.keywords),
            self.interests.strip(),
            self.max_duration_seconds,
            self.learning.duration_buckets,
        ]
        return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


class Article(BaseModel):
    id: str
    title: str
    url: str
    author: str = "未知"
    published: str = "未知"
    published_at: str | None = None
    excerpt: str = ""
    duration_seconds: int | None = None
    tags: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    matched_keywords: list[str] = Field(default_factory=list)
    parse_warnings: list[str] = Field(default_factory=list)
    fetched_at: str | None = None
    cover_url: str | None = None
    sources: list[SourceRef] = Field(default_factory=list)
    content_type: Literal["video", "text", "image", "article", "audio", "live", "unknown"] = "video"
    is_dynamic: bool = False
    dynamic_id: str | None = None
    author_uid: str | None = None
    original_author_uid: str | None = None
    activity_type: Literal["original", "repost"] = "original"
    activity_url: str | None = None
    forwarded_by: str | None = None
    original_published_at: str | None = None
    images: list[str] = Field(default_factory=list)
    images_status: Literal["known", "partial", "unknown"] = "unknown"
    attachments: list[dict] = Field(default_factory=list, max_length=8)
    attachments_status: Literal["known", "partial", "unknown"] = "unknown"
    repost_text: str = ""
    dynamic_contexts: list[dict] = Field(default_factory=list)
    text_available: bool = True
    filter_hits: list[dict] = Field(default_factory=list)
    expiry_basis: str | None = None
    body_text: str | None = Field(None, max_length=20000)
    body_truncated: bool = False

    @model_validator(mode="after")
    def non_video_duration(self):
        if self.content_type != "video":
            self.duration_seconds = None
        # Read older direct-version records without losing their repost attribution.
        original = next(
            (
                c
                for c in self.dynamic_contexts
                if c.get("id") != self.dynamic_id and c.get("author_uid") == self.author_uid
            ),
            None,
        )
        if self.is_dynamic and original is not None:
            self.activity_type = "repost"
            self.original_published_at = self.original_published_at or original.get("published_at")
            if self.content_type != "video" and original.get("url"):
                self.url = original["url"]
        if self.is_dynamic and self.dynamic_id:
            self.activity_url = self.activity_url or "https://t.bilibili.com/" + self.dynamic_id
        if self.activity_type == "repost" and not self.forwarded_by:
            outer = next((c for c in self.dynamic_contexts if c.get("id") == self.dynamic_id), {})
            self.forwarded_by = outer.get("author")
        return self

    @field_validator("images", mode="before")
    @classmethod
    def safe_images(cls, values):
        from .covers import content_image_url

        return list(dict.fromkeys(u for v in values or [] if (u := content_image_url(v))))[:12]

    @field_validator("cover_url", mode="before")
    @classmethod
    def safe_cover(cls, value):
        from .covers import cover_url

        return cover_url(value)


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    summary: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=300)
    action: str = Field(min_length=1, max_length=300)
    base_score: float = Field(50, ge=0, le=100, allow_inf_nan=False)
    eligible_regular: bool = True
    eligible_explore: bool = False
    exploration_reason: str = Field("", max_length=300)

    @model_validator(mode="after")
    def explore_reason(self):
        if self.eligible_explore and not self.exploration_reason.strip():
            raise ValueError("exploration requires evidence")
        return self


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    picks: list[Choice] = Field(default_factory=list, max_length=30)
