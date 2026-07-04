"""Architectural layering test.

Enforces the dependency direction of the package so the god-class never
quietly grows back. We parse each module with ``ast`` and inspect its
**runtime, module-level** internal imports — that is, we deliberately
ignore:

  - imports inside ``if TYPE_CHECKING:`` blocks (type-only, no runtime
    dependency), and
  - function-local / lazy imports (an accepted escape hatch for
    breaking import cycles, e.g. a read helper that needs the broker
    type at call time).

Invariants checked:

  domain/            → may import only domain/
  infrastructure/    → may import domain/, infrastructure/
  providers/         → may import domain/, infrastructure/, providers/
  execution/         → may import domain/, infrastructure/, execution/
  orders/            → + execution/, orders/ (NOT reads/ or corporate_actions/)
  reads/             → domain/, infrastructure/, execution/, reads/
  corporate_actions/ → domain/, infrastructure/, execution/, corporate_actions/
  workers/           → domain/, infrastructure/, execution/, workers/

  No inner-layer module imports the broker at module top level (only
  TYPE_CHECKING / lazy imports of ``broker`` are allowed).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parents[2] / "src" / "papertrade_india"

# The layered subpackages and what each is allowed to import (by first
# path segment). A layer may always import itself.
ALLOWED: dict[str, set[str]] = {
    "domain": {"domain"},
    "infrastructure": {"domain", "infrastructure"},
    "providers": {"domain", "infrastructure", "providers"},
    "execution": {"domain", "infrastructure", "execution"},
    "orders": {"domain", "infrastructure", "execution", "orders"},
    "reads": {"domain", "infrastructure", "execution", "reads"},
    "corporate_actions": {
        "domain", "infrastructure", "execution", "corporate_actions",
    },
    "workers": {"domain", "infrastructure", "execution", "workers"},
}

LAYERS = set(ALLOWED)


def _iter_layer_modules() -> list[tuple[str, Path]]:
    """Yield ``(layer, path)`` for every .py file inside a layered pkg."""
    out: list[tuple[str, Path]] = []
    for layer in LAYERS:
        for path in (_PKG_ROOT / layer).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            out.append((layer, path))
    return out


def _package_parts(path: Path) -> list[str]:
    """Dotted package parts of the file's containing package.

    e.g. ``.../papertrade_india/orders/state.py`` →
    ``["papertrade_india", "orders"]``.
    """
    rel = path.relative_to(_PKG_ROOT.parent)  # papertrade_india/orders/state.py
    parts = list(rel.with_suffix("").parts)   # [papertrade_india, orders, state]
    return parts[:-1]                          # drop the module name


def _resolve(level: int, module: str | None, pkg_parts: list[str]) -> str | None:
    """Resolve an import to its first segment under ``papertrade_india``.

    Returns the segment (e.g. ``"orders"``, ``"broker"``, ``"domain"``)
    or ``None`` if the import is external (not part of the package).
    """
    if level == 0:
        if not module or not module.startswith("papertrade_india"):
            return None
        tail = module.split(".")[1:]  # strip leading 'papertrade_india'
        return tail[0] if tail else None
    # Relative import: base = package_parts truncated by (level - 1).
    base = pkg_parts[: len(pkg_parts) - (level - 1)]
    if not base or base[0] != "papertrade_india":
        return None
    extra = module.split(".") if module else []
    full = base + extra  # e.g. [papertrade_india, domain, models]
    tail = full[1:]
    return tail[0] if tail else None


def _runtime_internal_targets(path: Path) -> list[tuple[str, int]]:
    """Module-level, non-TYPE_CHECKING internal import targets.

    Returns ``(segment, lineno)`` pairs. Descends into plain ``if``/
    ``try`` blocks but NOT into ``if TYPE_CHECKING:`` blocks, function
    bodies, or class bodies — so lazy/type-only imports are excluded.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    pkg_parts = _package_parts(path)
    targets: list[tuple[str, int]] = []

    def is_type_checking(node: ast.expr) -> bool:
        if isinstance(node, ast.Name) and node.id == "TYPE_CHECKING":
            return True
        return isinstance(node, ast.Attribute) and node.attr == "TYPE_CHECKING"

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.ImportFrom):
                seg = _resolve(node.level, node.module, pkg_parts)
                if seg is not None:
                    targets.append((seg, node.lineno))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("papertrade_india"):
                        tail = alias.name.split(".")[1:]
                        if tail:
                            targets.append((tail[0], node.lineno))
            elif isinstance(node, ast.If):
                if is_type_checking(node.test):
                    continue  # skip type-only imports
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, ast.Try):
                visit(node.body)
                for handler in node.handlers:
                    visit(handler.body)
                visit(node.orelse)
                visit(node.finalbody)
            # Deliberately do NOT descend into FunctionDef / AsyncFunctionDef
            # / ClassDef: those are lazy/runtime-local imports, allowed.

    visit(tree.body)
    return targets


@pytest.mark.parametrize(
    "layer,path",
    [pytest.param(lyr, p, id=str(p.relative_to(_PKG_ROOT))) for lyr, p in _iter_layer_modules()],
)
def test_layer_dependency_direction(layer: str, path: Path) -> None:
    """Each module imports only from its allowed layers (runtime, top-level)."""
    allowed = ALLOWED[layer]
    violations: list[str] = []
    for seg, lineno in _runtime_internal_targets(path):
        # Root-level modules (broker, _context, price_feed, quickstart,
        # presets, cli, interface, __init__) are the orchestration layer.
        # An inner layer must not import them at runtime — except the
        # broker check below is the one we care about most.
        if seg in LAYERS:
            if seg not in allowed:
                violations.append(f"  line {lineno}: imports '{seg}/' (not allowed)")
        elif seg == "broker":
            violations.append(
                f"  line {lineno}: imports the broker at module top level "
                f"(use TYPE_CHECKING or a function-local import instead)"
            )
        # Other root modules (_context, price_feed, presets, interface)
        # are shared collaborators and allowed.

    assert not violations, (
        f"{path.relative_to(_PKG_ROOT)} ({layer}/) has layering violations:\n"
        + "\n".join(violations)
    )


def test_peer_subsystems_do_not_import_each_other() -> None:
    """orders/, reads/, corporate_actions/ must stay mutually independent."""
    peers = {"orders", "reads", "corporate_actions"}
    problems: list[str] = []
    for layer, path in _iter_layer_modules():
        if layer not in peers:
            continue
        forbidden = peers - {layer}
        for seg, lineno in _runtime_internal_targets(path):
            if seg in forbidden:
                problems.append(
                    f"{path.relative_to(_PKG_ROOT)}:{lineno} imports '{seg}/'"
                )
    assert not problems, "Peer-subsystem cross-imports found:\n" + "\n".join(problems)


def test_domain_is_dependency_free() -> None:
    """domain/ is the innermost layer — it imports nothing else internal."""
    problems: list[str] = []
    for layer, path in _iter_layer_modules():
        if layer != "domain":
            continue
        for seg, lineno in _runtime_internal_targets(path):
            if (seg in LAYERS and seg != "domain") or seg == "broker":
                problems.append(
                    f"{path.relative_to(_PKG_ROOT)}:{lineno} imports '{seg}'"
                )
    assert not problems, "domain/ reached outward:\n" + "\n".join(problems)
