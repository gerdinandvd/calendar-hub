"""Log shape with actor and source_id, and redaction."""

import logging

import pytest

from app.log import log_mutation


def test_mutation_log_requires_actor_and_source_id() -> None:
    with pytest.raises(ValueError):
        log_mutation(actor="admin", source_id="source-1", message="done")
    with pytest.raises(ValueError):
        log_mutation(actor="owner", source_id="   ", message="done")


def test_mutation_log_keeps_actor_and_source_and_redacts(caplog) -> None:
    planted = (
        "Authorization: Basic abcdeftoken "
        "summary=SecretTitle description=SecretDescription "
        "location=SecretLocation Bearer ya29.super-secret-token"
    )
    with caplog.at_level(logging.INFO, logger="app"):
        log_mutation(actor="owner", source_id="source-1", message=planted)

    text = caplog.text
    assert "actor=owner" in text
    assert "source_id=source-1" in text
    for secret in (
        "abcdeftoken",
        "SecretTitle",
        "SecretDescription",
        "SecretLocation",
        "ya29.super-secret-token",
    ):
        assert secret not in text
