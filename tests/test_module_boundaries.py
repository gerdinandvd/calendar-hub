"""Guards the module boundaries from the design."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

ISOLATED = frozenset(
    {
        "app.sync.google",
        "app.sync.ics",
        "app.sync.webhook",
        "app.radicale.client",
    }
)
VEVENT_MARKER = "BEGIN:VEVENT"


def resolve_imports(source: str, module: str, is_package: bool) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            found.update(_resolve_from(node, module, is_package))
    return found


def _resolve_from(node: ast.ImportFrom, module: str, is_package: bool) -> set[str]:
    if node.level == 0:
        names: set[str] = set()
        if node.module:
            names.add(node.module)
            for alias in node.names:
                if alias.name != "*":
                    names.add(f"{node.module}.{alias.name}")
        return names

    parts = module.split(".")
    if not is_package:
        parts = parts[:-1]
    drop = node.level - 1
    if drop > len(parts):
        parts = []
    elif drop:
        parts = parts[: len(parts) - drop]
    if node.module:
        parts.extend(node.module.split("."))
    base = ".".join(parts)
    names = set()
    if base:
        names.add(base)
    for alias in node.names:
        if alias.name == "*":
            continue
        names.add(f"{base}.{alias.name}" if base else alias.name)
    return names


def _module_of(path: Path) -> tuple[str, bool]:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _matches(imported: set[str], targets: frozenset[str]) -> list[str]:
    hits = []
    for name in sorted(imported):
        for target in targets:
            if name == target or name.startswith(target + "."):
                hits.append(name)
                break
    return hits


def test_resolver_sees_absolute_and_relative_imports() -> None:
    absolute = resolve_imports(
        "from app.radicale.client import put\n",
        module="app.sync.google",
        is_package=False,
    )
    relative = resolve_imports(
        "from ..radicale.client import put\n",
        module="app.sync.google",
        is_package=False,
    )
    sibling = resolve_imports(
        "from . import ics\n",
        module="app.sync.google",
        is_package=False,
    )
    assert "app.radicale.client" in absolute
    assert "app.radicale.client" in relative
    assert "app.sync.ics" in sibling


def test_isolated_modules_do_not_import_each_other() -> None:
    violations: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        module, is_package = _module_of(path)
        if module not in ISOLATED:
            continue
        imported = resolve_imports(path.read_text(encoding="utf-8"), module, is_package)
        hits = _matches(imported, ISOLATED - {module})
        if hits:
            violations.append(f"{module} imports {', '.join(hits)}")
    assert violations == []


def test_ui_and_admin_do_not_import_isolated_modules() -> None:
    pages = APP / "ui" / "pages.py"
    imported = resolve_imports(
        pages.read_text(encoding="utf-8"),
        "app.ui.pages",
        False,
    )
    assert _matches(imported, ISOLATED) == []
    assert list((APP / "admin").rglob("*.py")) == []


def test_vevent_marker_only_allowed_in_ical() -> None:
    allowed = (APP / "sync" / "ical.py").resolve()
    marker = VEVENT_MARKER.encode("ascii")
    offenders = []
    for path in APP.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.resolve() == allowed:
            continue
        if marker in path.read_bytes():
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
