# knowledge_base/

Documents the Knowledge agent searches: store policies, supplier terms, product
catalogues, tax notes and FAQs. Written in Phase 1, embedded in Phase 3.

Each document is a Markdown file with front-matter, so every chunk keeps its
metadata and every answer can cite it:

```markdown
---
document_id: POL-CREDIT-001
title: Customer credit (udhaar) policy
source: internal
category: policy
version: 2
effective_date: 2026-04-01
---

## 1. Who can buy on credit
...
```

Keep one topic per file and use `##` headings for sections; the chunker splits on them.
