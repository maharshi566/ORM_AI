# data/

Generated and local-only data. Nothing here is committed except this file and `seed/`.

| Folder | What goes here | Phase |
| --- | --- | --- |
| `seed/` | Synthetic shop data as CSV or JSON: products, suppliers, sales, stock movements, customer credit | 1 |
| `chroma/` | ChromaDB's on-disk vector store, created by `scripts/ingest.py` (git-ignored) | 3 |

Never put real customer data in this folder.
