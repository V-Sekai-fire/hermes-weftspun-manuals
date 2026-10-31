"""weftspun-manuals: the weftspun manuals site as read-only memory, beside holographic memory.

Hermes allows one external memory provider, and holographic holds that seat. This plugin is therefore a
plain tool plugin plus a ``pre_llm_call`` recall hook: holographic keeps the writable facts, this keeps
the published record (RFDs, logbook, working agreements, blocklist). Nothing is configurable: the site,
the index file and the limits are constants below.

Two layers read the same site. The BM25 index (this file) gates recall and answers `get`/`list`; the
composed layer (composed.py) holds every passage as a read-only holographic fact with an HRR vector and
entity links, and answers `search`, `probe`, `reason` and `related` through holographic's own retriever.

Source: <BASE>search.json, the BM25 index the site's own search box loads (one entry per RFD, logbook
entry and agreement page, with the full text in ``b``). Cached under $HERMES_HOME/cache/weftspun-manuals/.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.request
from collections import Counter
from math import log
from pathlib import Path

from . import composed

logger = logging.getLogger(__name__)

BASE = "https://v-sekai-fire.github.io/manuals-weftspun/"
INDEX_URL = BASE + "search.json"
TTL_SECONDS = 6 * 3600          # refetch after this; a stale cache still serves when offline
FETCH_TIMEOUT = 8
K1, B = 1.2, 0.75               # same constants as the site's assets/search.js
RECALL_MAX_DOCS = 3
RECALL_MIN_SCORE = 6.0          # below this the turn is not about the manuals; inject nothing
RECALL_MIN_TERMS = 2
RECALL_SNIPPET = 240
RECALL_REPEAT_TURNS = 6
KIND_NAMES = {"rfd": "RFD", "log": "logbook", "page": "agreement"}

_lock = threading.Lock()
_index: dict | None = None      # {"docs": [...], "df": Counter, "avg": float, "loaded": float}
_shown: dict = {}               # session -> {url: turn}
_turns: Counter = Counter()


# ---- text ---------------------------------------------------------------------------------------
def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def _words(s: str) -> list:
    return re.findall(r"[^\W_]+", _fold(s))


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 3 and re.search(r"[^s]s$", w) else w


# ---- index --------------------------------------------------------------------------------------
def _cache_file() -> Path:
    from hermes_constants import get_hermes_home
    d = Path(get_hermes_home()) / "cache" / "weftspun-manuals"
    d.mkdir(parents=True, exist_ok=True)
    return d / "search.json"


def _download(path: Path) -> None:
    # Cache buster: GitHub Pages sits behind a CDN that serves a copy for ~10 minutes, so a bare URL can
    # return an edition older than the one just published. A fresh query value per fetch skips that copy;
    # the no-cache headers ask any proxy on the way to revalidate as well.
    req = urllib.request.Request(
        f"{INDEX_URL}?cb={time.time_ns()}",
        headers={"User-Agent": "hermes-weftspun-manuals/0.1",
                 "Cache-Control": "no-cache", "Pragma": "no-cache"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        raw = r.read()
    json.loads(raw)  # refuse to cache anything that is not JSON
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, path)


def _build(raw: list, loaded: float) -> dict:
    df: Counter = Counter()
    total = 0
    docs = []
    for d in raw:
        tf = Counter(_stem(w) for w in _words(d.get("b", "")))
        title = {_stem(w) for w in _words(d.get("t", ""))}
        for w in set(tf) | title:
            df[w] += 1
        n = sum(tf.values())
        total += n
        docs.append({**d, "tf": tf, "title": title, "len": n})
    return {"docs": docs, "df": df, "avg": (total / len(docs)) if docs else 1.0, "loaded": loaded}


def _load(force: bool = False) -> dict:
    """Return the index; refresh when older than TTL; fall back to a stale cache offline."""
    global _index
    with _lock:
        if _index and not force and time.time() - _index["loaded"] < TTL_SECONDS:
            return _index
        path = _cache_file()
        fresh = path.exists() and time.time() - path.stat().st_mtime < TTL_SECONDS
        err = None
        if force or not fresh:
            try:
                _download(path)
            except Exception as e:  # offline, DNS, 404: serve what we have
                err = e
                logger.warning("weftspun-manuals: fetch failed: %s", e)
        if not path.exists():
            raise RuntimeError(f"manuals index unavailable ({err}); nothing cached")
        raw = json.loads(path.read_text(encoding="utf-8"))
        _index = _build(raw, path.stat().st_mtime)
        _index["raw"] = raw
        _index["stale"] = err is not None
        return _index


# ---- search -------------------------------------------------------------------------------------
def _score(idx: dict, q_terms: list, kind=None) -> list:
    n = len(idx["docs"])
    out = []
    for d in idx["docs"]:
        if kind and d["k"] != kind:
            continue
        s, hit = 0.0, 0
        for t in q_terms:
            f = d["tf"].get(t, 0)
            in_title = t in d["title"]
            if not f and not in_title:
                continue
            hit += 1
            df = idx["df"].get(t, 0)
            idf = log(1 + (n - df + 0.5) / (df + 0.5))
            if f:
                s += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * d["len"] / idx["avg"]))
            if in_title:
                s += idf * 2.0
        if hit:
            out.append((s, d, hit))
    out.sort(key=lambda r: -r[0])
    return out


def _snippet(d: dict, q_terms: list, width: int = RECALL_SNIPPET) -> str:
    body = re.sub(r"\s+", " ", d.get("b", ""))
    low = _fold(body)
    pos = min((p for p in (low.find(t) for t in q_terms) if p >= 0), default=0)
    start = max(0, pos - width // 4)
    s = body[start:start + width].strip()
    return ("..." if start else "") + s + ("..." if start + width < len(body) else "")


def _url(d: dict) -> str:
    return BASE + d["u"]


def _label(d: dict) -> str:
    return f"[{KIND_NAMES.get(d['k'], d['k'])}{' ' + d['s'] if d.get('s') else ''}] {d['t']}"


# ---- tool ---------------------------------------------------------------------------------------
SCHEMA = {
    "name": "weftspun_manual",
    "description": (
        "Read-only memory of the weftspun manuals (https://v-sekai-fire.github.io/manuals-weftspun/): RFDs "
        "(decisions, design docs), the logbook (measurements), the working agreements, the blocklist. Use with "
        "fact_store: fact_store = what the user told you, weftspun_manual = what the workspace published.\n"
        "Every passage is also a read-only holographic fact (HRR vector, entity links), so queries compose.\n"
        "- search {query, kind?, limit?}: best passage per document, with URL and a bm25 score (0 = weak). kind: rfd | log | page.\n"
        "- probe {entity}: an RFD's own passages first, then every passage that mentions it (backlinks).\n"
        "- reason {entities: [a, b, ...]}: passages that mention ALL of them (e.g. two RFD ids).\n"
        "- related {entity}: passages structurally connected to a name, by HRR unbinding.\n"
        "- get {ref, offset?, max_chars?}: full text. ref: an RFD number ('2336' or 'RFD66606-2336'), a title "
        "fragment, or a path such as 'page-AGENTS.html' or 'logbook/<name>.html'.\n"
        "- list {kind?, status?, limit?}: titles and states (status is a substring of the state marks).\n"
        "- status: entry counts and cache age.\n"
        "- refresh: refetch the index now. The tool never writes to the site."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "probe", "reason", "related", "get", "list", "status", "refresh"]},
            "entity": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "query": {"type": "string"},
            "ref": {"type": "string"},
            "kind": {"type": "string", "enum": ["rfd", "log", "page"]},
            "status": {"type": "string"},
            "limit": {"type": "integer"},
            "offset": {"type": "integer"},
            "max_chars": {"type": "integer"},
        },
        "required": ["action"],
    },
}


def _resolve(idx: dict, ref: str):
    ref = ref.strip()
    if not ref:
        return None
    docs = idx["docs"]
    tail = ref.replace(BASE, "").lstrip("/")
    for d in docs:
        if d["u"].rstrip("/") == tail.rstrip("/"):
            return d
    m = re.fullmatch(r"(?:rfd)?[-_ ]?(?:66606-)?(\d{3,4})", ref, re.I)
    if m:
        want = f"RFD66606-{m.group(1)}"
        for d in docs:
            if d["k"] == "rfd" and d["t"].startswith(want + ":"):
                return d
    low = _fold(ref)
    cands = [d for d in docs if low in _fold(d["t"])]
    if cands:
        return min(cands, key=lambda d: len(d["t"]))
    return None


def _composed_ready(idx: dict) -> bool:
    """True when the holographic mirror can answer; start its (slow, one-time) build in the background if not."""
    if composed.retriever() is not None and composed.current(_cache_file().parent, idx["raw"]):
        return True
    if composed.current(_cache_file().parent, idx["raw"]):   # built already: opening it takes milliseconds
        return composed.ensure(_cache_file().parent, idx["raw"])
    if not composed.info()["building"]:
        threading.Thread(target=composed.ensure, args=(_cache_file().parent, idx["raw"]),
                         name="weftspun-manuals-mirror", daemon=True).start()
    return False


def _rows(facts: list, entity: str = "", limit: int = 8) -> list:
    rows = [composed.view(f) for f in facts]
    if entity:  # an RFD's own passages first, then the passages that mention it
        rows.sort(key=lambda r: r["doc"].lower() != entity.lower())
    return rows[:limit]


def _composed_action(action: str, args: dict, idx: dict, limit: int, kind) -> str:
    if not composed.available():
        return json.dumps({"error": "holographic plugin not found; composed actions need it (search still works "
                                    "through the BM25 index)"})
    if not _composed_ready(idx):
        return json.dumps({"error": "composed mirror is building (about two minutes, once per site change); "
                                    "retry shortly", **composed.info()})
    r = composed.retriever()
    cat = {"rfd": "rfd", "log": "logbook", "page": "agreement"}.get(kind)
    if action == "probe":
        ent = str(args.get("entity") or args.get("query") or "").strip()
        if not ent:
            return json.dumps({"error": "'entity' is required (an RFD id such as RFD66606-2294, or a name)"})
        facts = r.probe(ent, category=cat, limit=max(limit, 40))
        return json.dumps({"entity": ent, "results": _rows(facts, ent, limit)}, ensure_ascii=False)
    if action == "reason":
        ents = [e for e in (args.get("entities") or []) if isinstance(e, str) and e.strip()]
        if len(ents) < 2:
            return json.dumps({"error": "'entities' needs two or more names"})
        return json.dumps({"entities": ents, "results": _rows(r.reason(ents, category=cat, limit=limit))},
                          ensure_ascii=False)
    if action == "related":
        ent = str(args.get("entity") or args.get("query") or "").strip()
        if not ent:
            return json.dumps({"error": "'entity' is required"})
        return json.dumps({"entity": ent, "results": _rows(r.related(ent, category=cat, limit=limit))},
                          ensure_ascii=False)
    q = str(args.get("query") or "").strip()
    if not q:
        return json.dumps({"error": "'query' is required"})
    terms = list(dict.fromkeys(_stem(w) for w in _words(q)))
    bm = {d["u"]: s for s, d, _ in _score(idx, terms, kind)}
    seen, rows = set(), []
    for row in _rows(r.search(q, category=cat, limit=limit * 4), limit=limit * 4):
        if row["url"] in seen:
            continue
        seen.add(row["url"])
        row["bm25"] = round(bm.get(row["url"], 0.0), 2)   # 0 = no query word in the document: a weak match
        rows.append(row)
        if len(rows) == limit:
            break
    return json.dumps({"query": q, "results": rows}, ensure_ascii=False)


def _handle(args: dict, **_kw) -> str:
    try:
        action = (args.get("action") or "search").lower()
        idx = _load(force=action == "refresh")
        limit = max(1, min(int(args.get("limit") or 8), 60))
        kind = args.get("kind")
        if action in ("status", "refresh"):
            c = Counter(d["k"] for d in idx["docs"])
            if action == "refresh":
                _composed_ready(idx)  # rebuilds the mirror only when the site content changed
            return json.dumps({"source": BASE, "entries": dict(c), "total": len(idx["docs"]),
                               "cache_age_s": int(time.time() - idx["loaded"]), "stale": idx.get("stale", False),
                               "composed": composed.info()})
        if action in ("probe", "reason", "related"):
            return _composed_action(action, args, idx, limit, kind)
        if action == "list":
            st = args.get("status") or ""
            rows = [d for d in idx["docs"] if (not kind or d["k"] == kind) and st in (d.get("s") or "")]
            rows.sort(key=lambda d: d["t"])
            cap = limit if args.get("limit") else 60
            return json.dumps({"count": len(rows), "results": [
                {"title": d["t"], "state": d.get("s", ""), "changed": d.get("d", ""), "url": _url(d)}
                for d in rows[:cap]]}, ensure_ascii=False)
        if action == "get":
            d = _resolve(idx, str(args.get("ref") or ""))
            if not d:
                return json.dumps({"error": f"no entry for ref {args.get('ref')!r}; try action=search"})
            off = max(0, int(args.get("offset") or 0))
            mx = max(500, min(int(args.get("max_chars") or 12000), 60000))
            body = d.get("b", "")
            return json.dumps({"title": d["t"], "kind": d["k"], "state": d.get("s", ""), "changed": d.get("d", ""),
                               "url": _url(d), "total_chars": len(body), "offset": off,
                               "next_offset": off + mx if off + mx < len(body) else None,
                               "text": body[off:off + mx]}, ensure_ascii=False)
        if action == "search" and _composed_ready(idx):
            return _composed_action("search", args, idx, limit, kind)
        if action == "search":
            q = str(args.get("query") or "").strip()
            terms = list(dict.fromkeys(_stem(w) for w in _words(q)))
            if not terms:
                return json.dumps({"error": "'query' is required"})
            res = _score(idx, terms, kind)[:limit]
            return json.dumps({"query": q, "results": [
                {"title": d["t"], "kind": d["k"], "state": d.get("s", ""), "changed": d.get("d", ""),
                 "url": _url(d), "score": round(s, 2), "snippet": _snippet(d, terms)} for s, d, _ in res]},
                ensure_ascii=False)
        return json.dumps({"error": f"unknown action {action!r}"})
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


# ---- recall hook --------------------------------------------------------------------------------
def _recall(user_message=None, session_id: str = "", **_kw):
    """pre_llm_call: inject the best manuals hits for this turn, fenced, only when they clear the bar."""
    try:
        text = user_message if isinstance(user_message, str) else ""
        terms = list(dict.fromkeys(_stem(w) for w in _words(text) if len(w) > 2))
        if len(terms) < 2:
            return None
        if _index is None and not _cache_file().exists():
            return None  # never block a turn on a first download; the tool or the warm-up loads it
        idx = _load()
        _turns[session_id] += 1
        turn = _turns[session_id]
        seen = _shown.setdefault(session_id, {})
        picks = []
        for s, d, hit in _score(idx, terms):
            if s < RECALL_MIN_SCORE or hit < RECALL_MIN_TERMS:
                break
            if turn - seen.get(d["u"], -99) < RECALL_REPEAT_TURNS:
                continue
            picks.append(d)
            if len(picks) == RECALL_MAX_DOCS:
                break
        if not picks:
            return None
        passages = {}
        if composed.retriever() is not None and composed.current(_cache_file().parent, idx["raw"]):
            want = {d["u"] for d in picks}
            for f in composed.retriever().search(text, limit=60):
                v = composed.view(f)
                if v["url"] in want and v["url"] not in passages:
                    passages[v["url"]] = re.sub(r"^.*?, part \d+ of \d+: ", "", v["passage"], count=1)[:RECALL_SNIPPET]
        lines = ['<weftspun-manuals source="' + BASE + '">',
                 "Published record (read-only; holographic facts may be newer). Fetch more with "
                 "weftspun_manual get {ref}."]
        for d in picks:
            seen[d["u"]] = turn
            lines.append(f"- {_label(d)} {_url(d)}\n  {passages.get(d['u']) or _snippet(d, terms)}")
        lines.append("</weftspun-manuals>")
        return {"context": "\n".join(lines)}
    except Exception as e:  # recall must never break a turn
        logger.warning("weftspun-manuals recall skipped: %s", e)
        return None


def _warm() -> None:
    try:
        idx = _load()
        if composed.available():
            composed.ensure(_cache_file().parent, idx["raw"])
    except Exception as e:
        logger.warning("weftspun-manuals warm-up failed: %s", e)


def register(ctx) -> None:
    ctx.register_tool("weftspun_manual", "weftspun_manuals", SCHEMA, _handle, emoji="📖",
                      description=SCHEMA["description"])
    ctx.register_hook("pre_llm_call", _recall)
    threading.Thread(target=_warm, name="weftspun-manuals-warm", daemon=True).start()
