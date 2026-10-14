"""Content and layout invariants that Import Linter does not own.

Package-layer import contracts live in [tool.importlinter] (see
docs/python-toolchain.md). Keep only non-import checks here.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

VEVENT_MARKER = "BEGIN:VEVENT"


def test_admin_has_no_python() -> None:
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
