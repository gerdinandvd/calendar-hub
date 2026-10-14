"""Radicale rights file for ICS read-only collections."""

from __future__ import annotations

import configparser
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


def _parse_rules(text: str) -> list[tuple[str, str, str]]:
    """Return (user_pattern, collection_pattern, permissions) in file order."""

    parser = configparser.ConfigParser()
    parser.read_string(text)
    rules: list[tuple[str, str, str]] = []
    for section in parser.sections():
        rules.append(
            (
                parser.get(section, "user", fallback=""),
                parser.get(section, "collection"),
                parser.get(section, "permissions"),
            )
        )
    return rules


def _authorize(rules: list[tuple[str, str, str]], user: str, path: str) -> str | None:
    """First matching rule wins, like Radicale from_file (fullmatch on sane path)."""

    sane = path.strip("/")
    for user_pattern, collection_pattern, permissions in rules:
        if user_pattern and re.fullmatch(user_pattern, user) is None:
            continue
        if re.fullmatch(collection_pattern, sane) is None:
            continue
        return permissions
    return None


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


def test_rights_fullmatch_matrix_for_owner_and_internal() -> None:
    rules = _parse_rules(build_rights(owner="owner", internal="hub-internal"))
    cases = [
        ("", "R", "RrWw"),
        ("owner", "R", "RrWw"),
        ("owner/ics-d049d6d9-0a8b-40e8-8a18-f268c97be38e", "Rr", "RrWw"),
        ("owner/2db39297-b761-4432-8241-356ac9224285", "RrWw", "RrWw"),
    ]
    for path, owner_perm, internal_perm in cases:
        assert _authorize(rules, "owner", path) == owner_perm, path
        assert _authorize(rules, "hub-internal", path) == internal_perm, path


def test_ics_path_does_not_match_owner_rw_rule() -> None:
    rules = _parse_rules(build_rights(owner="owner", internal="hub-internal"))
    ics = "owner/ics-abc"
    rw_rule = next(rule for rule in rules if "(?!ics-)" in rule[1])
    assert re.fullmatch(rw_rule[1], ics) is None
    assert _authorize(rules, "owner", ics) == "Rr"


def test_dotted_owner_name_is_escaped_in_collection_patterns() -> None:
    text = build_rights(owner="user.name", internal="hub-internal")
    rules = _parse_rules(text)
    assert r"user\.name" in text
    assert _authorize(rules, "user.name", "user.name/ics-x") == "Rr"
    assert _authorize(rules, "user.name", "user.name/webhook-1") == "RrWw"
    assert _authorize(rules, "username", "user.name/ics-x") is None
