"""Corporate actions: splits, bonuses, rights, dividends.

The ``store`` module exposes the persistence record type
(``CorporateAction``) and CRUD helpers; sibling modules implement the
position/cash adjustments for each action type.
"""

from .store import CorporateAction

__all__ = ["CorporateAction"]
