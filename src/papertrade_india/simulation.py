"""Deprecated re-export shim — module moved to :mod:`papertrade_india.execution.simulation`.

Importing from here still works during the deprecation window but emits
a :class:`DeprecationWarning`. Update imports to the new path; this
shim will be removed in v0.3.
"""

from __future__ import annotations

import warnings as _warnings

_warnings.warn(
    "papertrade_india.simulation is deprecated; "
    "import from papertrade_india.execution.simulation instead.",
    DeprecationWarning,
    stacklevel=2,
)

import papertrade_india.execution.simulation as _src  # noqa: E402
from papertrade_india.execution.simulation import *  # noqa: E402, F401, F403

# Re-export every attribute defined on the new module — including
# underscore-prefixed names — so callers that imported private symbols
# during the refactor window keep working. Dunders are skipped.
globals().update(
    {name: getattr(_src, name) for name in dir(_src) if not name.startswith("__")}
)

del _src, _warnings
