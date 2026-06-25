"""Pure domain layer.

Contains value types, domain exceptions, and stateless rules. No I/O,
no infrastructure dependencies. Anything in this package must be safe
to load standalone, with zero internal imports from sibling subsystems.
"""
