"""Persistence, observability, clock, and other I/O concerns.

These modules can be imported by anyone, but must only depend on
``domain``. They are the outer ring of the application: they implement
the side effects that domain rules and use cases describe.
"""
