# data/

Generated and local-only data.

| Path | What it is | Committed? |
| --- | --- | --- |
| `seed/EDGE_CASES.md` | The 17 planted situations the agents must handle, with record IDs | yes |
| `seed/csv/` | One CSV per table, exported by `python -m scripts.seed --export-dir data/seed/csv` | yes (for browsing) |
| `chroma/` | ChromaDB's on-disk vector store, created by `scripts/ingest.py` (Phase 3) | no |

The CSVs and EDGE_CASES.md are regenerated every time you run the seed command with
`--export-dir`. If you change the generator, re-export so they stay in step with the
database. Never put real customer data in this folder.
