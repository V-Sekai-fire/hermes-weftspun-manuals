# weftspun-manuals

A Hermes plugin that exposes the weftspun manuals (https://v-sekai-fire.github.io/manuals-weftspun/) as read-only memory beside the holographic provider.

Hermes allows one external memory provider and holographic holds it, so this is a tool plugin with a recall hook:

- `weftspun_manual` tool: `search`, `probe`, `reason`, `related`, `get`, `list`, `status`, `refresh` over the RFDs, the logbook and the working agreements.
- `pre_llm_call` hook: adds up to three fenced hits to a turn when the message clearly matches the manuals, and nothing otherwise.

The site and every limit are constants at the top of `__init__.py`. The plugin downloads the site's own `search.json` (BM25 index with full text), adds a cache buster to every fetch so the Pages CDN cannot serve an older edition, caches it under `$HERMES_HOME/cache/weftspun-manuals/` for six hours, and serves the stale copy when offline.

Install: clone into `$HERMES_HOME/plugins/weftspun-manuals`, then `hermes plugins enable weftspun-manuals`.

## Composed layer (read-only holographic facts)

Every entry is split into passages of about 650 characters, and each passage becomes one fact in a separate mirror database, `manuals_store.db`, in holographic's schema: HRR vector, entity links, FTS. Each fact links to its own RFD id and to every RFD id it mentions, so `probe RFD66606-2294` returns that RFD plus every passage that mentions it, and `reason [a, b]` returns the passages that mention both.

- The mirror is derived from `search.json` and rebuilt (about two minutes, in the background) only when the site content changes. No tool action writes to it, and `memory_store.db` is never opened.
- Queries run through holographic's own `MemoryStore` and `FactRetriever`, loaded from `$HERMES_HOME/plugins/holographic`, so there is one copy of the HRR code. Without the holographic plugin, `search` falls back to the BM25 index and `probe`, `reason` and `related` report that they need it.
- Holographic's search score ranks only; it does not measure relevance. Recall therefore gates on BM25 and shows the composed passages for the documents that pass.
