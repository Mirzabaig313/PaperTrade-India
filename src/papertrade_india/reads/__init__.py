"""Read-only views over broker state.

Modules here expose query helpers (positions, account, orders) that
never mutate state. Used by the broker façade to back the public read
methods.
"""
