"""Order lifecycle: submission, queueing, fills, state transitions.

Modules here orchestrate the path from ``buy()``/``sell()`` through
validation, dispatch, fill, position update, and event emission.
"""
