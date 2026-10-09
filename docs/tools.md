# Tools reference

Agents act on the shop only through these tools. The registry
(`backend/app/tools/registry.py`) decides which agent may call which tool, checks
the arguments, runs the tool with a timeout, retries safe failures, and logs every
call to the `tool_calls` table.

## Read tools: the Data retrieval agent

They return **facts only**. Deciding what the facts mean under a policy is the
Investigation agent's job (Phase 4).

| Tool | Answers | Notable fields |
| --- | --- | --- |
| `search_products` | "Which products match 'dal'?" | name, stock, reorder level, price |
| `get_product` | "Tell me about PRD-0085" | cost, margin %, preferred supplier, open orders, units sold in 30 days |
| `get_low_stock_products` | "What needs reordering?" | each low item with its open orders, if any |
| `get_slow_moving_products` | "What hasn't sold in 90 days?" | stock value at cost, last sale |
| `get_stock_movements` | "Why is the rice count off?" | every recent sale, delivery, return, damage and adjustment |
| `get_sales_summary` | "How did September go?" | bills, total, discounts, GST, by payment mode, top 5 products |
| `get_sale` | "Show bill SALE-000538" | items, customer, status, days since sale |
| `search_customers` | "Find Priya" or "...0101" | phone is masked to the last 3 digits |
| `get_customer_account` | "What does CUST-0002 owe?" | balance, limit, unpaid bills oldest first with days past due, last payment, reminders, open disputes |
| `get_purchase_orders` | "What's on order?" | status, expected date, days late, items |
| `get_supplier` | "What are Deccan's terms?" | lead time, payment terms, minimum order, late or short orders |
| `get_case_history` | "Has this happened before?" | past cases and how they were resolved |
| `check_supplier_price` | "Is the ghee cost right?" | quote from the (mock) supplier API vs recorded cost, price and MRP |

## Knowledge tool: the Knowledge agent

| Tool | Answers | Notable fields |
| --- | --- | --- |
| `search_knowledge` | "What does the shop's credit policy say about limits?" | passages with `citation` (`[POL-CREDIT-001 v2 §2. Credit limits]`), `status`, `trust`, `suspicious`; a ready-made `context` block; `found: false` with a note when nothing matches |

Arguments: `query`, optional `categories`, `top_k` (1-10, default 5),
`include_superseded` and `include_untrusted` (both off by default). The shop and
the date come from the context, so a shop only ever sees its own profile and the
rules in effect. Details: [rag.md](rag.md).

## Action tools: the Action agent

| Tool | Does | Approval needed | Refuses when (policy) |
| --- | --- | --- | --- |
| `create_purchase_order` | Draft or send an order to one supplier | Draft: none. Send: staff up to Rs 10,000, owner above | an item already has an open order; product inactive; below the supplier's minimum order |
| `record_stock_adjustment` | Count correction or damage | Staff up to Rs 1,000 at cost, owner above | stock would go below zero |
| `send_payment_reminder` | WhatsApp/SMS reminder | none (it follows the reminder policy exactly) | owes nothing; not 7+ days past due; disputed; reminded in the last 7 days; 3 unanswered reminders; paid in the last 3 days; outside 9 am to 8 pm |
| `update_selling_price` | Change a selling price | Always the owner | above MRP; below cost (warns when margin is under 8%) |
| `process_return` | Return a whole bill, restock, refund | Staff up to Rs 2,000, owner above | older than 7 days; already returned |
| `follow_up_supplier` | Message the supplier about a late or short purchase order, and say whether the owner must now be told (POL-SUPPLIER-001, SOP-SHORT-001) | Staff | not late yet, or nothing missing; the order is not this shop's; already messaged about it in the last 24 hours |
| `create_case` | Open a case to track a problem | none | (an identical open case is reused) |
| `resolve_case` | Close a case with its resolution | Staff | |

Every action:

- needs an `idempotency_key`. Reusing it returns the first result with
  `replayed: true`,
- writes an `audit_logs` row naming who did it and who approved it,
- runs in one transaction: it either fully happens or not at all.

The order of checks is deliberate: **validate, then shop rules, then approval**.
A person is never asked to approve something the rules would block anyway.

## Error codes

| Code | Meaning | What the agent should do |
| --- | --- | --- |
| `invalid_input` | Arguments don't match the schema | Fix the arguments |
| `not_found` | No such record **in this shop** | Ask the user for the right ID |
| `forbidden` | This agent may not use this tool | Hand over to the right agent |
| `conflict` | e.g. an order is already open | Use the existing record |
| `policy_blocked` | A shop rule forbids it (`error_details` says which) | Explain the rule to the user |
| `approval_required` | A person must approve (`required_role`: staff or owner) | Send to the approval step (Phase 5) |
| `timeout`, `upstream_error`, `rate_limited` | Temporary failure (`retryable: true`) | Reads were already retried twice; tell the user and try later |
| `internal_error` | A bug | Report it; details are in the server log only |

## Adding a tool

1. Write an input model (subclass `ToolInput`), an output model (subclass
   `ToolOutput`) and an `async def handler(ctx, args)`. Look records up with the
   helpers in `tools/helpers.py`, which already filter by shop.
2. For an action: check the idempotency key first, then validate, then the shop
   rules, then call `ctx.require_approval(...)`, then make the change and call
   `audit(...)`.
3. Add a `ToolSpec` to the module's list (`DATABASE_TOOLS`, `API_TOOLS` or
   `BUSINESS_TOOLS`) and the name to `AGENT_TOOLS` if needed.
4. Write tests in `tests/test_tools_*.py` for success, each refusal and the
   approval levels.

## Failure injection

Set in `.env` (or per test with `FaultInjector`):

```
MOCK_API_FAILURE_MODE=none | timeout | server_error | not_found | rate_limited | random
MOCK_API_LATENCY_MS=0
```

`random` fails about 1 in 5 calls with a server error or a timeout. It is useful
for seeing how the agents cope (Phase 8 evaluates exactly this).
