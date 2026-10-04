"""Write (action) tools. Only the Action agent may call these.

Planned: create_purchase_order, record_stock_adjustment,
send_payment_reminder, update_price. Every call takes an idempotency key so a
resumed workflow can never do the same thing twice.
"""

# TODO(phase-2): implement action tools with idempotency keys.
