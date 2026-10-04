"""Data retrieval agent.

Fetches shop records through read-only tools: products and stock levels, sales,
purchase orders, suppliers and the customer credit ledger. It never invents a
value a tool did not return. Tools come from app/tools (Phase 2).
"""

# TODO(phase-4): call the allowlisted read tools and store results with their tool-call IDs.
