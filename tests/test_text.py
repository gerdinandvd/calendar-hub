"""Catalog keys, English API text, and stored sentences."""

from app.sync.apply import CAPACITY_MESSAGE, SAVE_MESSAGE
from app.sync.google import (
    FETCH_MESSAGE as GOOGLE_FETCH,
    NOT_CONFIGURED,
    NOT_CONNECTED,
    READ_MESSAGE as GOOGLE_READ,
    SCOPE_MESSAGE,
    TOKEN_MESSAGE,
)
from app.sync.ics import FETCH_MESSAGE, READ_MESSAGE, SIZE_MESSAGE, TIMEOUT_MESSAGE
from app.sync.runner import PROCESS_STOPPED
from app.ui.text import EN, english, present

_CODES = (
    SAVE_MESSAGE,
    CAPACITY_MESSAGE,
    FETCH_MESSAGE,
    TIMEOUT_MESSAGE,
    SIZE_MESSAGE,
    READ_MESSAGE,
    GOOGLE_FETCH,
    GOOGLE_READ,
    TOKEN_MESSAGE,
    NOT_CONNECTED,
    NOT_CONFIGURED,
    SCOPE_MESSAGE,
    PROCESS_STOPPED,
    "sentence.not_found",
    "sentence.sync_unused",
    "sentence.sync_off",
    "sentence.process_stopped",
    "sentence.feed_fetch",
)


def test_sync_codes_are_catalog_keys() -> None:
    for code in _CODES:
        assert code in EN
        assert code.startswith("sentence.")


def test_english_renders_keys_and_leaves_unknown_text() -> None:
    assert english("sentence.enter_name") == "Enter a name."
    assert english("sentence.enter_name sentence.timezone") == (
        "Enter a name. Enter a valid time zone."
    )
    assert english("The owner account is not configured.") == (
        "The owner account is not configured."
    )
    assert english("") == ""


def test_present_translates_keys_legacy_sentences_and_unknown_text() -> None:
    assert present("en", "sentence.event_saved") == "The event could not be saved."
    assert present("nl", "sentence.event_saved") == "Het event kon niet worden opgeslagen."
    assert present("nl", "sentence.enter_name sentence.timezone") == (
        "Vul een naam in. Vul een geldige tijdzone in."
    )
    assert present("nl", "The event could not be saved.") == "Het event kon niet worden opgeslagen."
    assert present("nl", "Radicale is not reachable.") == "Radicale is niet bereikbaar."
    assert present("nl", "A custom operator note.") == "A custom operator note."
    assert present("en", "") == ""
