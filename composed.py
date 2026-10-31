"""Composed layer: the manuals as read-only holographic facts.

The site's entries are split into short standalone passages and written, by holographic's own store, into
a mirror database (``manuals_store.db`` beside the search cache) in holographic's schema: one fact per
passage, an HRR vector per fact, entity links, FTS. The mirror is derived: it is rebuilt from search.json
whenever that file changes, and no tool action writes to it. Queries go through holographic's own
retriever (search, probe, reason, related), so composition works the way it does for the user's facts:
probe an RFD id and get that RFD plus every passage that mentions it; reason over two ids and get the
passages that mention both.

Holographic is loaded from $HERMES_HOME/plugins/holographic (one source of truth, no second copy of the
HRR code). If it is missing, ``available()`` is false and the caller falls back to the BM25 index.
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import os
import re
import sqlite3
import sys
import threading
import types
from pathlib import Path

logger = logging.getLogger(__name__)

ALIAS = "_weftspun_holo"       # private package name; never collides with the live provider's module
PASSAGE_CHARS = 650
RFD_ID = re.compile(r"RFD66606-\d{3,4}")
TRUST = {"✅": 0.9, "📢": 0.8, "💬": 0.6, "📝": 0.5, "💡": 0.4}
STATE_WORDS = {"✅": "committed", "📢": "published", "💬": "discussion", "📝": "prediscussion", "💡": "ideation",
               "➡️": "moved", "🪦": "abandoned"}
KIND_TRUST = {"log": 0.6, "page": 0.9}

_lock = threading.Lock()
_state: dict = {"store": None, "retriever": None, "path": None, "building": False, "error": None, "facts": 0}


def _modules():
    from hermes_constants import get_hermes_home
    d = Path(get_hermes_home()) / "plugins" / "holographic"
    if not (d / "store.py").exists():
        raise RuntimeError(f"holographic plugin not found at {d}")
    if sys.modules.get(ALIAS) is None:
        pkg = types.ModuleType(ALIAS)
        pkg.__path__ = [str(d)]
        sys.modules[ALIAS] = pkg
    return (importlib.import_module(ALIAS + ".store"), importlib.import_module(ALIAS + ".retrieval"))


def available() -> bool:
    try:
        _modules()
        return True
    except Exception as e:
        logger.warning("weftspun-manuals: holographic unavailable: %s", e)
        return False


def _passages(body: str) -> list:
    text = re.sub(r"\s+", " ", body).strip()
    if not text:
        return []
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        while len(sent) > PASSAGE_CHARS:           # a sentence longer than a passage: hard split on a space
            cut = sent.rfind(" ", 0, PASSAGE_CHARS) or PASSAGE_CHARS
            if cur:
                out.append(cur); cur = ""
            out.append(sent[:cut]); sent = sent[cut:].strip()
        if cur and len(cur) + 1 + len(sent) > PASSAGE_CHARS:
            out.append(cur); cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        out.append(cur)
    return out


def _doc_id(d: dict) -> str:
    m = RFD_ID.match(d["t"])
    return m.group(0) if m else d["t"]


def _fingerprint(raw: list) -> str:
    h = hashlib.sha256()
    for d in raw:
        h.update(f"{d['u']}\0{d['t']}\0{d.get('s','')}\0{d['b']}\0".encode("utf-8"))
    return h.hexdigest()[:16]


def mirror_path(cache_dir: Path) -> Path:
    return cache_dir / "manuals_store.db"


def _stamp(path: Path) -> Path:
    return path.with_suffix(".sha")


def current(cache_dir: Path, raw: list) -> bool:
    p = mirror_path(cache_dir)
    return p.exists() and _stamp(p).exists() and _stamp(p).read_text().strip() == _fingerprint(raw)


def build(cache_dir: Path, raw: list) -> int:
    """Write the mirror from raw search.json entries; returns the fact count. Atomic: build aside, then replace."""
    store_mod, _ = _modules()
    final = mirror_path(cache_dir)
    tmp = cache_dir / "manuals_store.building.db"
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(str(tmp) + suffix)
        except FileNotFoundError:
            pass
    st = store_mod.MemoryStore(tmp)
    n = 0
    try:
        for d in raw:
            marks = d.get("s", "") or ""
            state = next((STATE_WORDS[k] for k in STATE_WORDS if k in marks), "")
            trust = next((TRUST[k] for k in TRUST if k in marks), KIND_TRUST.get(d["k"], 0.5))
            doc = _doc_id(d)
            label = d["t"] + (f" ({state})" if state else "")
            parts = _passages(d["b"]) or [d["t"]]
            tags = ",".join(x for x in (d["k"], state, d.get("d", ""), "url:" + d["u"], "doc:" + doc) if x)
            category = {"rfd": "rfd", "log": "logbook", "page": "agreement"}.get(d["k"], d["k"])
            for i, chunk in enumerate(parts, 1):
                content = f"{label}, part {i} of {len(parts)}: {chunk}"
                ents = [doc, d["t"]] + sorted(set(RFD_ID.findall(chunk)) - {doc})
                st.add_fact_status(content, category=category, tags=tags, trust=trust, entities=ents)
                n += 1
    finally:
        st.close()
    con = sqlite3.connect(str(tmp))
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(str(tmp) + suffix)
        except FileNotFoundError:
            pass
    with _lock:
        _state["store"] = _state["retriever"] = None   # drop handles so the replace is not blocked on Windows
    os.replace(tmp, final)
    _stamp(final).write_text(_fingerprint(raw))
    return n


def ensure(cache_dir: Path, raw: list) -> bool:
    """Open (building first if stale) the mirror. Returns True when queries can run."""
    with _lock:
        if _state["building"]:
            return False
        if _state["retriever"] is not None and current(cache_dir, raw):
            return True
        _state["building"] = True
    try:
        if not current(cache_dir, raw):
            _state["facts"] = build(cache_dir, raw)
        store_mod, retr_mod = _modules()
        st = store_mod.MemoryStore(mirror_path(cache_dir), migrate=False)   # read-only context: creates nothing
        with _lock:
            _state.update(store=st, retriever=retr_mod.FactRetriever(st), path=str(mirror_path(cache_dir)), error=None)
        return True
    except Exception as e:
        logger.warning("weftspun-manuals: composed mirror failed: %s", e)
        _state["error"] = f"{type(e).__name__}: {e}"
        return False
    finally:
        _state["building"] = False


def retriever():
    return _state["retriever"]


def info() -> dict:
    return {"ready": _state["retriever"] is not None, "building": _state["building"], "error": _state["error"],
            "facts": _state["facts"]}


def view(fact: dict) -> dict:
    """A retriever fact as a tool result row: doc, state, URL, passage."""
    tags = {}
    for t in (fact.get("tags") or "").split(","):
        if t.startswith("url:"):
            tags["url"] = t[4:]
        elif t.startswith("doc:"):
            tags["doc"] = t[4:]
    return {"doc": tags.get("doc", ""), "kind": fact.get("category"), "url": tags.get("url", ""),
            "fact_id": fact.get("fact_id"), "trust": round(float(fact.get("trust_score") or 0), 2),
            "score": round(float(fact.get("score") or 0), 3), "passage": fact.get("content", "")}
