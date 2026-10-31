# weftspun-manuals

A Hermes plugin that exposes the weftspun manuals (https://v-sekai-fire.github.io/manuals-weftspun/) as read-only memory beside the holographic provider.

Hermes allows one external memory provider and holographic holds it, so this is a tool plugin with a recall hook:

- `weftspun_manual` tool: `search`, `get`, `list`, `status`, `refresh` over the RFDs, the logbook and the working agreements.
- `pre_llm_call` hook: adds up to three fenced hits to a turn when the message clearly matches the manuals, and nothing otherwise.

The site and every limit are constants at the top of `__init__.py`. The plugin downloads the site's own `search.json` (BM25 index with full text), adds a cache buster to every fetch so the Pages CDN cannot serve an older edition, caches it under `$HERMES_HOME/cache/weftspun-manuals/` for six hours, and serves the stale copy when offline.

Install: clone into `$HERMES_HOME/plugins/weftspun-manuals`, then `hermes plugins enable weftspun-manuals`.
