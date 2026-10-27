"""One versioned settings authority for trusted hosts and the local web UI."""

import hashlib
import json
import uuid
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from .agent_migrations import goal_id
from .agent_models import PreferenceChange, PreferenceView
from .core_errors import CoreError
from .schemas import InterestIdentity, Profile
from .storage import Store, atomic_write, dump


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def scope(path, profile_id, account_scope):
    return digest(str(Path(path).resolve()).casefold() + "|" + profile_id + "|" + account_scope)


def version(profile):
    from .retrieval import hash_data

    return hash_data(profile.model_dump())


def view(profile):
    return PreferenceView(
        profile_id=profile.profile_id,
        version=version(profile),
        keywords=profile.keywords,
        interests=profile.interests,
        goal_id=goal_id(profile),
        learning_context=profile.context,
        keyword_search_enabled=profile.source_plan.keyword_search.enabled,
    )


def fields(before, after):
    a, b = before.model_dump(), after.model_dump()
    result = []
    for key in a:
        if key in ("sources", "schema_version", "interests", "interest_identity"):
            continue
        if key == "source_plan":
            if a[key]["keyword_search"] != b[key]["keyword_search"]:
                result.append("keyword_search")
            if {k: v for k, v in a[key].items() if k != "keyword_search"} != {
                k: v for k, v in b[key].items() if k != "keyword_search"
            }:
                result.append("source_plan")
            continue
        if a[key] != b[key]:
            result.append(key)
    if a["interests"] != b["interests"] or a["interest_identity"] != b["interest_identity"]:
        result.append("interest")
    return result


def read_fields(profile, keys):
    data = profile.model_dump()
    return {
        key: dict(interests=data["interests"], interest_identity=data["interest_identity"])
        if key == "interest"
        else data["source_plan"]["keyword_search"]
        if key == "keyword_search"
        else {k: v for k, v in data["source_plan"].items() if k != "keyword_search"}
        if key == "source_plan"
        else data[key]
        for key in keys
    }


def apply_interest(before, data, text, mode):
    if text is None or text == before.interests and mode != "switch_goal":
        return
    if mode == "same_goal":
        identity = before.interest_identity or InterestIdentity(
            goal_id=goal_id(before), legacy_anchor=before.interests.strip()
        )
    elif mode == "switch_goal":
        identity = InterestIdentity(goal_id="goal-" + uuid.uuid4().hex)
    else:
        raise CoreError("INVALID_ARGUMENT")
    data.update(interests=text, interest_identity=identity.model_dump())


def _pending_busy(db):
    # The application serializes settings writes with collection/briefing jobs.
    return False


def _finalize(store, row):
    db = store.db
    path = Path(row["profile_path"])
    before = Profile.model_validate(json.loads(row["before_text"]))
    after = Profile.model_validate(json.loads(row["after_text"]))
    current = (
        Profile.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))
        if path.exists()
        else before
    )
    current_versions = {version(current), digest(dump(current.model_dump()))}
    if not current_versions.intersection((row["before_version"], row["after_version"])):
        db.execute("UPDATE setting_changes SET status='conflict' WHERE id=?", (row["id"],))
        return "SETTINGS_RECOVERY_CONFLICT"
    if row["before_version"] in current_versions and _pending_busy(db):
        return "SETTINGS_BUSY"
    if row["after_version"] not in current_versions or not path.exists():
        atomic_write(path, row["after_text"] + "\n")
    changed = json.loads(row["fields"])
    previous = json.loads(row["before_heads"])
    for field in changed:
        restored = previous.get(field) if row["undo_of"] else row["id"]
        if restored:
            db.execute(
                "INSERT OR REPLACE INTO setting_heads VALUES(?,?,?)",
                (row["namespace"], field, restored),
            )
        else:
            db.execute(
                "DELETE FROM setting_heads WHERE namespace=? AND field=?", (row["namespace"], field)
            )
    gid = goal_id(after)
    db.execute(
        "INSERT OR IGNORE INTO interest_goals VALUES(?,?,?,?,?)",
        (
            after.profile_id,
            gid,
            after.context,
            row["created"],
            "explicit_" + ("undo" if row["undo_of"] else "revision"),
        ),
    )
    if "interest" in changed:
        db.execute(
            "INSERT OR IGNORE INTO interest_revisions VALUES(?,?,?,?,?,?)",
            (row["id"], after.profile_id, gid, after.context, after.interests, row["created"]),
        )
    promoted = 0
    if "keywords" in changed:
        from .briefing import local_now
        from .pool import namespace, upsert
        from .retrieval import normalize

        new = [term for term in after.keywords if term not in before.keywords]
        rows = db.execute(
            "SELECT * FROM temporary_items WHERE namespace=? AND julianday(last_seen)>=julianday(?)",
            (namespace(after), (local_now() - timedelta(hours=24)).isoformat()),
        ).fetchall()
        for term in new:
            for temp in rows:
                if normalize(temp["keyword"]) == normalize(term):
                    from datetime import datetime

                    ref = dict(kind="keyword_search", key=term, label="关键词 · " + term)
                    upsert(
                        db,
                        after,
                        ref,
                        json.loads(temp["data"]),
                        datetime.fromisoformat(temp["last_seen"]),
                    )
                    db.execute(
                        "UPDATE pool_items SET first_seen=? WHERE namespace=? AND kind='keyword_search' AND source_key=? AND entry_key=?",
                        (temp["first_seen"], namespace(after), term, temp["entry_key"]),
                    )
                    obs = db.execute(
                        "SELECT * FROM source_observations WHERE namespace=? AND kind='temporary_keyword' AND source_key=?",
                        (namespace(after), temp["keyword"]),
                    ).fetchone()
                    old = db.execute(
                        "SELECT checked FROM source_observations WHERE namespace=? AND kind='keyword_search' AND source_key=?",
                        (namespace(after), term),
                    ).fetchone()
                    if obs and (not old or obs["checked"] >= old["checked"]):
                        db.execute(
                            "INSERT OR REPLACE INTO source_observations VALUES(?,?,?,?,?,?,?,?)",
                            (
                                namespace(after),
                                "keyword_search",
                                term,
                                obs["checked"],
                                obs["last_success"],
                                obs["status"],
                                obs["count"],
                                obs["error_code"],
                            ),
                        )
                    promoted += 1
    new_terms = [x for x in after.keywords if x not in before.keywords]
    from .pool import namespace

    ready = all(
        db.execute(
            "SELECT 1 FROM pool_items WHERE namespace=? AND kind='keyword_search' AND source_key=? LIMIT 1",
            (namespace(after), k),
        ).fetchone()
        for k in new_terms
    )
    response = PreferenceChange(
        change_id=row["id"],
        before=view(before),
        after=view(after),
        changed_fields=changed,
        needs_collection=bool(new_terms) and not ready,
        data_ready=ready,
        promoted_items=promoted,
        next_action="enable_keyword_search"
        if new_terms and not after.source_plan.keyword_search.enabled
        else "refresh_collection"
        if new_terms and not ready
        else "none",
        undo_of=row["undo_of"],
    )
    db.execute(
        "UPDATE setting_changes SET status='committed',response=? WHERE id=?",
        (response.model_dump_json(), row["id"]),
    )
    return None


def recover_pending(store):
    # Durable intent is written before file replacement. Completing that intent is idempotent.
    for row in store.db.execute(
        "SELECT id FROM setting_changes WHERE status='prepared' ORDER BY rowid"
    ).fetchall():
        store.db.execute("BEGIN IMMEDIATE")
        try:
            record = store.db.execute(
                "SELECT * FROM setting_changes WHERE id=?", (row["id"],)
            ).fetchone()
            if record["status"] == "prepared":
                _finalize(store, record)
            store.db.commit()
        except (OSError, ValueError, ValidationError):
            store.db.rollback()
        except Exception:
            store.db.rollback()
            raise


class SettingsService:
    def __init__(self, root, profile_path=None, clock=None, default_profile=None):
        self.root = Path(root).resolve()
        self.path = Path(profile_path or self.root / "briefing_profile.json").resolve()
        if not self.path.is_relative_to(self.root):
            raise CoreError("PROFILE_SCOPE_DENIED")
        self.db_path = self.root / "workspace/bilibili_shixun.sqlite3"
        if clock is None:
            from .briefing import local_now

            clock = local_now
        self.clock = clock
        self.default_profile = default_profile

    def load(self):
        if not self.path.exists() and self.default_profile is not None:
            return Profile.model_validate(self.default_profile())
        return Profile.model_validate(json.loads(self.path.read_text(encoding="utf-8-sig")))

    def get(self, profile_id):
        with closing(Store(self.db_path, clock=self.clock, rebuild_preferences=False)) as store:
            recover_pending(store)
            p = self.load()
            if p.profile_id != profile_id:
                raise CoreError("PROFILE_SCOPE_DENIED")
            return view(p)

    def _commit(
        self, store, before, after, request_key, payload_hash, undo_of=None, undo_heads=None
    ):
        changed = fields(before, after)
        space = scope(self.path, before.profile_id, before.account_scope)
        previous = {
            key: (
                store.db.execute(
                    "SELECT change_id FROM setting_heads WHERE namespace=? AND field=?",
                    (space, key),
                ).fetchone()
                or [None]
            )[0]
            for key in changed
        }
        if undo_heads is not None:
            previous = undo_heads
        change_id = uuid.uuid4().hex
        store.db.execute(
            "INSERT INTO setting_changes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                change_id,
                space,
                request_key,
                payload_hash,
                self.clock().isoformat(),
                "prepared",
                str(self.path),
                dump(before.model_dump()),
                dump(after.model_dump()),
                version(before),
                version(after),
                dump(changed),
                dump(previous),
                None,
                undo_of,
            ),
        )
        store.db.commit()  # durable file outbox; claim() refuses new work while this intent is pending
        store.db.execute("BEGIN IMMEDIATE")
        try:
            record = store.db.execute(
                "SELECT * FROM setting_changes WHERE id=?", (change_id,)
            ).fetchone()
            error = _finalize(store, record) if record["status"] == "prepared" else None
            store.db.commit()
            if error:
                raise CoreError(error)
            response = store.db.execute(
                "SELECT response FROM setting_changes WHERE id=?", (change_id,)
            ).fetchone()[0]
            return PreferenceChange.model_validate_json(response)
        except CoreError:
            raise
        except Exception:
            store.db.rollback()
            raise CoreError("SETTINGS_IO") from None

    def _replay(self, store, space, key, payload_hash):
        row = store.db.execute(
            "SELECT * FROM setting_changes WHERE namespace=? AND request_key=?", (space, key)
        ).fetchone()
        if not row:
            return None
        if row["payload_hash"] != payload_hash:
            raise CoreError("IDEMPOTENCY_CONFLICT")
        if row["status"] == "conflict":
            raise CoreError("SETTINGS_RECOVERY_CONFLICT")
        if row["status"] != "committed":
            raise CoreError("SETTINGS_BUSY")
        return PreferenceChange.model_validate_json(row["response"]).model_copy(
            update={"replayed": True}
        )

    def update(self, request):
        payload_hash = digest(request.model_dump_json())
        with closing(Store(self.db_path, clock=self.clock, rebuild_preferences=False)) as store:
            recover_pending(store)
            store.db.execute("BEGIN IMMEDIATE")
            try:
                before = self.load()
                if before.profile_id != request.profile_id:
                    raise CoreError("PROFILE_SCOPE_DENIED")
                space = scope(self.path, before.profile_id, before.account_scope)
                replay = self._replay(store, space, request.request_id, payload_hash)
                if replay:
                    store.db.rollback()
                    return replay
                if version(before) != request.expected_version:
                    raise CoreError("SETTINGS_CONFLICT")
                if _pending_busy(store.db):
                    raise CoreError("SETTINGS_BUSY")
                add = [x.strip() for x in request.add_keywords]
                remove = [x.strip() for x in request.remove_keywords]
                from .text_evidence import normalize

                added, removed = {normalize(x) for x in add}, {normalize(x) for x in remove}
                if any(not x or len(x) > 100 for x in add + remove) or added & removed:
                    raise CoreError("INVALID_ARGUMENT")
                data = before.model_dump()
                values = [x for x in before.keywords if normalize(x) not in removed] + add
                unique = {}
                for term in values:
                    unique.setdefault(normalize(term), term)
                data["keywords"] = list(unique.values())
                if request.keyword_search_enabled is not None:
                    data["source_plan"]["keyword_search"]["enabled"] = (
                        request.keyword_search_enabled
                    )
                if not data["keywords"]:
                    data["source_plan"]["keyword_search"]["enabled"] = False
                apply_interest(before, data, request.interests, request.interest_mode)
                data["schema_version"] = 4
                try:
                    after = Profile.model_validate(data)
                except ValidationError:
                    raise CoreError("SETTINGS_INVALID") from None
                return self._commit(store, before, after, request.request_id, payload_hash)
            except Exception:
                if store.db.in_transaction:
                    store.db.rollback()
                raise

    def set_subscription_enabled(self, request):
        payload_hash = digest(request.model_dump_json())
        with closing(Store(self.db_path, clock=self.clock, rebuild_preferences=False)) as store:
            recover_pending(store)
            store.db.execute("BEGIN IMMEDIATE")
            try:
                before = self.load()
                if before.profile_id != request.profile_id:
                    raise CoreError("PROFILE_SCOPE_DENIED")
                space = scope(self.path, before.profile_id, before.account_scope)
                replay = self._replay(store, space, request.request_id, payload_hash)
                if replay:
                    store.db.rollback()
                    return replay
                if version(before) != request.expected_version:
                    raise CoreError("SETTINGS_CONFLICT")
                if _pending_busy(store.db):
                    raise CoreError("SETTINGS_BUSY")
                data = before.model_dump()
                plan = data["source_plan"]
                if request.source == "keyword_search":
                    if request.enabled and not before.keywords:
                        raise CoreError("SUBSCRIPTION_KEYWORDS_REQUIRED")
                    plan["keyword_search"]["enabled"] = request.enabled
                elif request.source == "up":
                    target = next((u for u in plan["ups"] if u["uid"] == request.up_uid), None)
                    if target is None:
                        raise CoreError("SUBSCRIPTION_NOT_FOUND")
                    target["enabled"] = request.enabled
                else:
                    target = plan["following"]
                    if request.enabled:
                        if not request.account_uid or request.account_uid != before.account_scope:
                            raise CoreError("PROFILE_SCOPE_DENIED")
                        target["uid"] = request.account_uid
                    target["enabled"] = request.enabled
                    if request.following_scope is not None:
                        target["scope"] = request.following_scope
                after = Profile.model_validate(data)
                return self._commit(store, before, after, request.request_id, payload_hash)
            except Exception:
                if store.db.in_transaction:
                    store.db.rollback()
                raise

    def web_update(self, body):
        payload_hash = digest(dump(body))
        key = body.get("request_id") or uuid.uuid4().hex
        with closing(Store(self.db_path, clock=self.clock, rebuild_preferences=False)) as store:
            recover_pending(store)
            store.db.execute("BEGIN IMMEDIATE")
            try:
                before = self.load()
                space = scope(self.path, before.profile_id, before.account_scope)
                replay = self._replay(store, space, key, payload_hash)
                if replay:
                    store.db.rollback()
                    return replay
                if _pending_busy(store.db):
                    raise CoreError("SETTINGS_BUSY")
                from .retrieval import hash_data

                if body.get("expected_version") != hash_data(before.model_dump()):
                    raise CoreError("SETTINGS_CONFLICT")
                allowed = {
                    "keywords",
                    "interests",
                    "max_picks",
                    "max_duration_seconds",
                    "exploration_ratio",
                    "history_cooldown_days",
                    "snooze_days",
                    "sources",
                    "source_plan",
                    "filters",
                    "collection",
                    "learning_enabled",
                }
                patch = body.get("changes", {})
                if (
                    not isinstance(patch, dict)
                    or set(patch) - allowed
                    or "sources" in patch
                    and "source_plan" in patch
                ):
                    raise CoreError("INVALID_ARGUMENT")
                data = before.model_dump()
                data.update(
                    {k: v for k, v in patch.items() if k not in ("interests", "learning_enabled")}
                )
                if "sources" in patch:
                    data.pop("source_plan", None)
                if "learning_enabled" in patch:
                    data["learning"]["enabled"] = patch["learning_enabled"]
                # Legacy clients retain their old switch semantics; new UI sends the mode explicitly.
                apply_interest(
                    before, data, patch.get("interests"), body.get("interest_mode", "switch_goal")
                )
                data["schema_version"] = 4
                after = Profile.model_validate(data)
                return self._commit(store, before, after, key, payload_hash)
            except Exception:
                if store.db.in_transaction:
                    store.db.rollback()
                raise

    def undo(self, request):
        with closing(Store(self.db_path, clock=self.clock, rebuild_preferences=False)) as store:
            recover_pending(store)
            store.db.execute("BEGIN IMMEDIATE")
            try:
                current = self.load()
                if current.profile_id != request.profile_id:
                    raise CoreError("PROFILE_SCOPE_DENIED")
                space = scope(self.path, current.profile_id, current.account_scope)
                request_key = request.request_id or "undo:" + request.change_id
                payload_hash = (
                    digest(request.model_dump_json())
                    if request.request_id
                    else digest(request.change_id)
                )
                replay = self._replay(store, space, request_key, payload_hash)
                if replay:
                    return replay
                if (
                    request.expected_version is not None
                    and version(current) != request.expected_version
                ):
                    raise CoreError("SETTINGS_CONFLICT")
                original = store.db.execute(
                    "SELECT * FROM setting_changes WHERE id=? AND namespace=? AND status='committed'",
                    (request.change_id, space),
                ).fetchone()
                if not original:
                    raise CoreError("PREFERENCE_CHANGE_NOT_FOUND")
                previous = store.db.execute(
                    "SELECT response FROM setting_changes WHERE namespace=? AND undo_of=?",
                    (space, original["id"]),
                ).fetchone()
                if previous and previous[0]:
                    if request.request_id:
                        raise CoreError("PREFERENCE_UNDO_CONFLICT")
                    return PreferenceChange.model_validate_json(previous[0]).model_copy(
                        update={"replayed": True}
                    )
                changed = json.loads(original["fields"])
                if (
                    not changed
                    or original["undo_of"]
                    or set(changed) & {"account_scope", "profile_id"}
                ):
                    raise CoreError("PREFERENCE_UNDO_CONFLICT")
                if _pending_busy(store.db):
                    raise CoreError("SETTINGS_BUSY")
                before = Profile.model_validate(json.loads(original["before_text"]))
                after = Profile.model_validate(json.loads(original["after_text"]))
                if read_fields(current, changed) != read_fields(after, changed):
                    raise CoreError("PREFERENCE_UNDO_CONFLICT")
                for field in changed:
                    head = store.db.execute(
                        "SELECT change_id FROM setting_heads WHERE namespace=? AND field=?",
                        (space, field),
                    ).fetchone()
                    if not head or head[0] != original["id"]:
                        raise CoreError("PREFERENCE_UNDO_CONFLICT")
                data = current.model_dump()
                for field, value in read_fields(before, changed).items():
                    if field == "interest":
                        data.update(value)
                    elif field == "keyword_search":
                        data["source_plan"]["keyword_search"] = value
                    elif field == "source_plan":
                        data["source_plan"].update(value)
                    else:
                        data[field] = value
                try:
                    restored = Profile.model_validate(data)
                except ValidationError:
                    raise CoreError("PREFERENCE_UNDO_CONFLICT") from None
                old_heads = {
                    k: v for k, v in json.loads(original["before_heads"]).items() if k in changed
                }
                return self._commit(
                    store,
                    current,
                    restored,
                    request_key,
                    payload_hash,
                    undo_of=original["id"],
                    undo_heads=old_heads,
                )
            finally:
                if store.db.in_transaction:
                    store.db.rollback()
