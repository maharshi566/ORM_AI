"""Typed tools the agents use to read and change shop records.

* ``base.py``: the contract (ToolSpec, ToolResult, ToolContext, ToolError, approvals)
* ``database_tools.py``: read-only lookups (Data retrieval agent)
* ``api_tools.py``: mock supplier and messaging APIs with failure injection
* ``business_tools.py``: actions that change records (Action agent)
* ``registry.py``: the single entry point that validates, runs, retries and logs calls
"""
