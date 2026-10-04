---
document_id: FAQ-003
title: FAQ — stock and orders
source: internal
category: faq
version: 1
effective_date: 2026-01-01
status: current
---

# FAQ: stock and orders

## How does ORM_AI know the stock?

Every sale, delivery, return, damage entry and count adjustment is recorded as a stock movement. Current stock is the sum of those movements.

## What is the reorder level?

The stock level at which a product should be reordered. When stock is at or below it, the product appears in the reorder suggestions.

## Why didn't ORM_AI suggest reordering a low item?

It found an open order for it already, or the product has not sold in 90 days, or it is marked inactive (POL-REORDER-001).

## A delivery is late. Should we order from someone else?

Follow up first. After 7 days late, the owner decides (POL-SUPPLIER-001).

## Counted stock is lower than the records. What now?

Recount, then record an adjustment with the reason (SOP-COUNT-001). If the same product is short twice in 3 months, the owner investigates.
