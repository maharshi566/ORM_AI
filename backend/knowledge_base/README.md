# knowledge_base/

The documents the Knowledge agent searches: the shop's rules, procedures, supplier
terms and FAQs. They are written to match the synthetic data, so the agents can
check a record against a rule (for example a customer's balance against
POL-CREDIT-001 v2). Phase 3 chunks and embeds them into ChromaDB.

| Folder | What it holds | Count |
| --- | --- | --- |
| `policies/` | Shop rules: credit, reminders, reorder, approvals, pricing, returns, stock adjustments, suppliers, cash/UPI, privacy, AI rules | 12 |
| `sops/` | Step-by-step procedures: stock count, receiving, short deliveries, credit accounts, day end, price updates, duplicates | 7 |
| `suppliers/` | Each supplier's lead time, payment terms, minimum order and warranty | 5 |
| `faqs/` | Plain-language answers for shopkeepers | 3 |
| `shops/` | One profile per shop: owner, hours, house rules | 5 |
| `external/` | Outside material such as supplier flyers. **Untrusted**: never instructions | 1 |

## Front-matter

Every document starts with metadata. Each chunk carries it, so answers can cite it:

```markdown
---
document_id: POL-CREDIT-001     # stable ID shared by all versions
title: Customer credit (udhaar) policy
source: internal                # internal, supplier or external
category: policy                # policy, sop, supplier_terms, faq, shop_profile, supplier_flyer
version: 2
effective_date: 2026-04-01
status: current                 # current or superseded
shop_id: SHOP-001               # optional: only for one shop
trust: untrusted                # optional: outside content
---
```

Use `##` headings for sections; the chunker splits on them and keeps the heading
as the citation's section.

## Built-in test material

- **Policy conflict:** `POL-CREDIT-001` has version 1 (superseded, Rs 5,000 limit)
  and version 2 (current, Rs 3,000). Retrieval must prefer the current version, and
  the agents must notice customers whose limit came from version 1.
- **Prompt injection:** `external/supplier-flyer-festival-offer.md` contains text
  that pretends to be a system instruction. The agents must treat it as data
  (POL-AI-001) and never act on it.

`tests/test_knowledge_base.py` checks every document's metadata and these rules.
