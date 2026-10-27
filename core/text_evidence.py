"""Bounded original-text windows. Offsets always address one retained source field."""

import hashlib
import json
import unicodedata
from bisect import bisect_left


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold().strip()


def clean_terms(values):
    """Shared explicit-term contract. Never derive terms from question text."""
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError("terms must be a string list")
    terms = []
    for value in values:
        term = normalize(value)
        if not term:
            continue
        if len(term) > 100:
            raise ValueError("term too long")
        if term not in terms:
            terms.append(term)
    if len(terms) > 8:
        raise ValueError("too many terms")
    return terms


def normalized_offsets(text):
    """Map compatibility expansion, casefold and composing sequences back to raw slices.

    Split only at normalization-independent boundaries, including Hangul composition.
    Combining runs are normalized together; never treat a folded offset as a raw index.
    """
    output = []
    offsets = []
    cluster = ""
    start = 0

    def emit(end):
        value = unicodedata.normalize("NFKC", cluster).casefold()
        output.append(value)
        offsets.extend([(start, end)] * len(value))

    for i, char in enumerate(text):
        if not cluster:
            cluster = char
            start = i
            continue
        if unicodedata.combining(char):
            cluster += char
            continue
        if unicodedata.normalize("NFKC", cluster + char) == unicodedata.normalize(
            "NFKC", cluster
        ) + unicodedata.normalize("NFKC", char):
            emit(i)
            cluster = char
            start = i
        else:
            cluster += char
    if cluster:
        emit(len(text))
    return "".join(output), offsets


def hits(text, groups):
    if not groups:
        return []
    folded, offsets = normalized_offsets(text)
    found = set()
    for group, terms in enumerate(groups):
        for term in terms:
            if not term:
                continue
            cursor = folded.find(term)
            while cursor >= 0:
                found.add((offsets[cursor][0], offsets[cursor + len(term) - 1][1], group))
                cursor = folded.find(term, cursor + 1)
    return sorted(found)


def documents(article, source):
    body_field = "body_text" if article.get("body_text") else "excerpt"
    fields = [(body_field, article.get(body_field, ""))]
    fields.extend(
        (f"dynamic_contexts[{i}].text", c.get("text", ""))
        for i, c in enumerate(article.get("dynamic_contexts", []))
        if not (
            c.get("text") == article.get(body_field)
            and c.get("author_uid")
            and c.get("author_uid") == article.get("author_uid")
        )
    )
    fields.append(("title", article["title"]))
    fields.extend((f"tags[{i}]", tag) for i, tag in enumerate(article.get("tags", [])))
    fields.append(("author", article.get("author", "")))
    return [
        dict(field=name, text=text, source=source)
        for name, text in fields
        if isinstance(text, str) and text
    ]


def split_interval(doc, start, end, limit, overlap=40):
    """Split real contiguous slices; overlap is bounded and charged by the caller."""
    result = []
    while start < end:
        stop = min(end, start + limit)
        if stop < end:
            # Prefer a reasonably full sentence/line, never normalize the text.
            cut = max(
                (
                    i + 1
                    for i in range(start + max(1, limit // 2), stop)
                    if doc["text"][i] in "\n。！？；.!?;"
                ),
                default=stop,
            )
            stop = cut
        result.append(span(doc, start, stop))
        if stop == end:
            break
        start = stop - min(overlap, max(0, (stop - start) // 4))
    return result


def content_pack(entry, groups, budget, snippet_limit):
    """Base title/body first, then lexical windows, independent allowed origins.

    Selection budgets include split overlap. Intersecting intervals are merged
    only within one contribution and field before final selectable slices exist.
    """

    def stable(c):
        return (c["proof"]["kind"], c["proof"]["key"], c["entry_key"])

    ranked = []
    for c in entry["contributions"]:
        docs = documents(c["article"], c["proof"])
        covered = {g for d in docs for _, _, g in hits(d["text"], groups)}
        ranked.append(dict(contribution=c, docs=docs, groups=covered))
    ranked.sort(key=lambda row: (-len(row["groups"]), stable(row["contribution"])))
    primary = ranked[0]
    ordered = [primary]
    ranges = []
    used = set()
    remaining = budget

    def slices(values):
        return [s for d, lo, hi in values for s in split_interval(d, lo, hi, snippet_limit)]

    def add(doc, lo, hi):
        nonlocal ranges, remaining
        if lo >= hi or remaining <= 0:
            return

        # Fit the longest affordable prefix, counting overlap in resulting slices.
        def proposed(end):
            merged_lo, merged_hi = lo, end
            position = len(ranges)
            joined = set()
            changed = True
            while changed:
                changed = False
                for index, (old, a, b) in enumerate(ranges):
                    if index not in joined and old is doc and a <= merged_hi and merged_lo <= b:
                        merged_lo = min(merged_lo, a)
                        merged_hi = max(merged_hi, b)
                        position = min(position, index)
                        joined.add(index)
                        changed = True
            keep = [value for index, value in enumerate(ranges) if index not in joined]
            keep.insert(min(position, len(keep)), (doc, merged_lo, merged_hi))
            return keep

        best = None
        low = lo + 1
        high = hi
        # Boundary preference makes costs slightly non-monotonic; a bounded scan
        # guarantees exact fitting without changing any retained original text.
        for end in range(high, low - 1, -1):
            candidate = proposed(end)
            cost = sum(len(s["text"]) for s in slices(candidate))
            if cost <= budget:
                best = candidate
                break
        if best is None:
            return
        ranges = best
        remaining = budget - sum(len(s["text"]) for s in slices(ranges))
        for s in slices(ranges):
            used.update(g for _, _, g in hits(s["text"], groups))

    def base(row):
        title = next((d for d in row["docs"] if d["field"] == "title"), None)
        if title:
            add(title, 0, min(300, len(title["text"])))
        body = next((d for d in row["docs"] if d["field"] in ("body_text", "excerpt")), None)
        if body:
            add(body, 0, min(len(body["text"]), 600 if len(body["text"]) <= 600 else 200))
            if len(body["text"]) > 600:
                windows(row, [body])

    def windows(row, docs):
        if not remaining:
            return
        local = dict(documents=docs, groups=groups, contributions=[row["contribution"]])
        values, _ = select_spans(local, groups, budget)
        for value in values:
            doc = next(d for d in docs if d["field"] == value["field"])
            add(doc, value["start"], value["end"])

    base(primary)
    rest = ranked[1:]
    while rest:
        rest.sort(
            key=lambda row: (
                -len(row["groups"] - used),
                -len(row["groups"]),
                stable(row["contribution"]),
            )
        )
        row = rest.pop(0)
        ordered.append(row)
        base(row)
    for row in ordered:
        for doc in row["docs"]:
            if not doc["field"].startswith("dynamic_contexts["):
                continue
            if len(doc["text"]) <= 600:
                add(doc, 0, len(doc["text"]))
            else:
                windows(row, [doc])
    output = slices(ranges)
    for i, s in enumerate(output, 1):
        s["evidence_ref"] = "E" + str(i)
    actual = {(id(d), i) for d, lo, hi in ranges for i in range(lo, hi)}
    total = sum(
        len(d["text"])
        for row in ordered
        for d in row["docs"]
        if d["field"] not in ("author",) and not d["field"].startswith("tags[")
    )
    limited = len(actual) < total or any(
        row["contribution"]["article"].get("body_truncated", False)
        or row["contribution"]["article"].get("body_text") is None
        for row in ordered
    )
    return output, limited, primary["contribution"]


def span(doc, start, end):
    text = doc["text"][start:end]
    stable_source = {
        k: v for k, v in doc["source"].items() if k not in ("collected_at", "query_id")
    }
    identity = json.dumps(
        [stable_source, doc["field"], start, end, text], sort_keys=True, ensure_ascii=False
    )
    return dict(
        id=hashlib.sha256(identity.encode()).hexdigest()[:24],
        field=doc["field"],
        start=start,
        end=end,
        text=text,
        source=doc["source"],
    )


def select_spans(entry, groups, limit):
    """Greedy group coverage, then total groups, then stable source/field/offset order.

    Every window retains up to 80 surrounding raw characters per side. Disjoint
    windows remain separate, including their source and raw offsets.
    """
    docs = entry["documents"]
    candidates = []
    radius = min(80, max(0, limit // (2 * max(1, len(groups)) + 2)))
    for index, doc in enumerate(docs):
        matches = doc.get("hits") if groups == entry.get("groups") else None
        if matches is None:
            matches = hits(doc["text"], groups)
        by_group = {g: sorted((s, e) for s, e, k in matches if k == g) for g in range(len(groups))}
        starts = {g: [s for s, e in values] for g, values in by_group.items()}

        def covered(start, end):
            result = set()
            for g, values in by_group.items():
                i = bisect_left(starts[g], start)
                if i < len(values) and values[i][1] <= end:
                    result.add(g)
            return result

        seen = set()
        for start, end, group in matches:
            lo = max(0, start - radius)
            hi = min(len(doc["text"]), end + radius)
            if (lo, hi) not in seen:
                seen.add((lo, hi))
                candidates.append(
                    dict(doc=index, start=lo, end=hi, anchor=(start, end), groups=covered(lo, hi))
                )
    if not groups and docs:
        candidates = [
            dict(doc=0, start=0, end=min(limit, len(docs[0]["text"])), anchor=(0, 0), groups=set())
        ]
    selected = []
    remaining = limit
    used = set()
    while candidates and remaining:
        candidates.sort(
            key=lambda c: (
                -len(c["groups"] - used),
                -len(c["groups"]),
                c["doc"],
                c["start"],
                c["end"],
            )
        )
        chosen = None
        for c in candidates:
            if c["anchor"][1] - c["anchor"][0] > remaining:
                continue
            lo = c["start"]
            hi = c["end"]
            if hi - lo > remaining:
                lo = max(
                    lo,
                    min(
                        c["anchor"][0]
                        - max(0, (remaining - (c["anchor"][1] - c["anchor"][0])) // 2),
                        hi - remaining,
                    ),
                )
                hi = lo + remaining
            # Context may overlap. Dropping an overlapping window outright could
            # hide a new hit just outside the previous one (e.g. two words 100
            # characters apart with 80-character context). Only skip shown hits.
            if any(
                c["doc"] == x["doc"] and x["start"] <= c["anchor"][0] and c["anchor"][1] <= x["end"]
                for x in selected
            ):
                continue
            present = (
                {g for s, e, g in hits(docs[c["doc"]]["text"][lo:hi], groups)} if groups else set()
            )
            chosen = dict(c, start=lo, end=hi, groups=present)
            break
        if chosen is None:
            break
        selected.append(chosen)
        used.update(chosen["groups"])
        remaining -= chosen["end"] - chosen["start"]
        candidates = [
            c
            for c in candidates
            if not (
                c["doc"] == chosen["doc"]
                and chosen["start"] <= c["anchor"][0]
                and c["anchor"][1] <= chosen["end"]
            )
        ]
    # Preserve selection priority, but do not manufacture continuity between fields/sources.
    output = [span(docs[c["doc"]], c["start"], c["end"]) for c in selected]
    total = sum(len(d["text"]) for d in docs)
    limited = any(
        c["article"].get("body_truncated", False) or c["article"].get("body_text") is None
        for c in entry["contributions"]
    )
    return output, limited or sum(len(s["text"]) for s in output) < total


def detail_body(entry, limit):
    """Fair prefix reading of allowed contributions; delimiters are never evidence."""
    from .chat_output import attribution

    groups, seen = [], set()
    for contribution in entry["contributions"]:
        docs = documents(contribution["article"], contribution["proof"])
        preferred = [
            d
            for d in docs
            if d["field"] not in ("title", "author") and not d["field"].startswith("tags[")
        ]
        unique = []
        for doc in preferred:
            owner = attribution(entry, doc)
            identity = (doc["text"], owner.get("author"), owner.get("url"), owner["role"])
            if identity not in seen:
                seen.add(identity)
                unique.append(doc)
        groups.append(unique)
    body = ""
    parts = []
    total = sum(len(d["text"]) for group in groups for d in group)
    separator = "\n\n[独立来源片段]\n"
    for i, group in enumerate(groups):
        allowance = max(0, (limit - len(body)) // (len(groups) - i))
        spent = 0
        for doc in group:
            prefix = separator if body else ""
            size = min(len(doc["text"]), allowance - spent - len(prefix))
            if size <= 0:
                break
            body += prefix
            start = len(body)
            body += doc["text"][:size]
            spent += len(prefix) + size
            parts.append(
                dict(
                    field=doc["field"],
                    start=0,
                    end=size,
                    body_start=start,
                    body_end=len(body),
                    source=doc["source"],
                )
            )
    limited = any(
        c["article"].get("body_truncated", False) or c["article"].get("body_text") is None
        for c in entry["contributions"]
    )
    return body, parts, limited or sum(p["end"] - p["start"] for p in parts) < total
