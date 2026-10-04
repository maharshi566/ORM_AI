"""Action agent.

Carries out approved or low-risk actions through write tools only: create a
purchase order, record a stock adjustment, send a payment reminder, update a
price. An action counts as done only when its tool returns success.
"""

# TODO(phase-5): run approved actions after the human-approval node.
