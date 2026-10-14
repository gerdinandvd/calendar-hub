"""Radicale rights file for ICS read-only collections."""

import importlib.util
import re
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "radicale_rights",
    Path(__file__).resolve().parents[1] / "deploy" / "radicale_rights.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)
build_rights = _MOD.build_rights


def test_owner_ics_paths_are_read_only_and_others_writable() -> None:
    text = build_rights(owner="owner", internal="hub-internal")
    assert "type =" not in text
    assert "user: ^owner$" in text
    assert "user: ^hub\\-internal$" in text
    assert "collection: .*\npermissions: RrWw" in text
    assert "collection: ^owner$\npermissions: R" in text
    assert "collection: ^owner/ics-.*\npermissions: Rr" in text
    assert "collection: ^owner/(?!ics-).*\npermissions: RrWw" in text
    assert text.index("^owner/ics-") < text.index("^owner/(?!ics-)")
    assert re.fullmatch(r"^owner/ics-.*", "owner/ics-abc")
    assert re.fullmatch(r"^owner/(?!ics-).*", "owner/webhook-1")
    assert re.fullmatch(r"^owner/(?!ics-).*", "owner/ics-abc") is None
