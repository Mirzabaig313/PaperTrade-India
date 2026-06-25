"""Stateless validation rules over orders and positions.

Each rule module exposes a small, pure surface (typically a ``check`` or
``validate`` function) that subsystems compose in order pipelines.
"""
