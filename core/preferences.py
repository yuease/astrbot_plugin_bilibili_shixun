"""Conservative, deterministic local learning; no model and no inferred authors."""

import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def duration_feature(article, policy):
    seconds = article.get("duration_seconds")
    if seconds is None:
        return None
    index = sum(seconds > boundary for boundary in policy.duration_buckets)
    return "duration:" + str(index)


def features(article, policy):
    tags = sorted(set(t.strip().casefold() for t in article.get("tags", []) if t.strip()))[
        : policy.max_tags
    ]
    return ["tag:" + t for t in tags]


def refresh(store, profile, now):
    policy = profile.learning
    latest = {}
    for f in store.effective_feedback(profile.context):
        if f["value"] == "later":
            # A snooze on a later recommendation is not a new interest evaluation.
            continue
        if f["video"] not in latest or f["seq"] > latest[f["video"]]["seq"]:
            latest[f["video"]] = f
    evidence = defaultdict(list)
    for f in latest.values():
        article = json.loads(f["snapshot"])["article"]
        reason = (f["reason"] or "").strip()
        sign = 1 if f["value"] == "useful" else -1
        pairs = []
        if not reason:
            tags = features(article, policy)
            pairs = [(tag, sign / len(tags)) for tag in tags]
        elif reason in ("太长", "too_long", "太短", "too_short"):
            feature = duration_feature(article, policy)
            if feature:
                pairs = [(feature, -1)]
        day = datetime.fromisoformat(f["created"]).astimezone(SHANGHAI).date()
        for feature, contribution in pairs:
            evidence[feature].append(
                dict(
                    event=f["id"], video=f["video"], day=day.isoformat(), contribution=contribution
                )
            )
    weights = {}
    today = now.astimezone(SHANGHAI).date()
    for feature, entries in evidence.items():
        days = defaultdict(float)
        for e in entries:
            days[e["day"]] += e["contribution"]
        daily = [
            max(-policy.daily_cap, min(policy.daily_cap, net))
            * 2
            ** (-max(0, (today - datetime.fromisoformat(day).date()).days) / policy.half_life_days)
            for day, net in days.items()
        ]
        net = sum(daily)
        total = sum(abs(x) for x in daily)
        consistency = (total + abs(net)) / (2 * total) if total else 0
        if (
            len({e["video"] for e in entries}) >= policy.activation_min_videos
            and len(days) >= policy.activation_min_days
            and abs(net) >= policy.activation_net
            and consistency >= policy.activation_consistency
        ):
            weights[feature] = max(
                -policy.max_score_adjustment, min(policy.max_score_adjustment, net)
            )
    return store.save_preferences(profile.context, now, weights, dict(evidence))


def refresh_safe(store, profile, now):
    try:
        head = store.db.execute(
            "SELECT pinned FROM preference_heads WHERE context=?", (profile.context,)
        ).fetchone()
        if head and head[0]:
            return (*store.preferences(profile.context), None)
        return (*refresh(store, profile, now), None)
    except Exception as exc:
        # The feedback transaction is already committed; next startup retries.
        version, weights = store.preferences(profile.context)
        return version, weights, type(exc).__name__
