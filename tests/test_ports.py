"""Smoke imports for ports protocols and domain re-export shims."""

from app import ports
from app.ports import (
    AuditLogger,
    CalendarStore,
    Cipher,
    Clock,
    FeedFetcher,
    MappingRepository,
    RateLimiterPort,
    SecretStore,
    SourceRepository,
    SyncCoordinator,
    SyncStateRepository,
    TokenRepository,
    UnitOfWork,
)


def test_ports_package_exports() -> None:
    for name in (
        "CalendarStore",
        "FeedFetcher",
        "SyncCoordinator",
        "Clock",
        "AuditLogger",
        "Cipher",
        "RateLimiterPort",
        "SourceRepository",
        "MappingRepository",
        "SyncStateRepository",
        "TokenRepository",
        "SecretStore",
        "UnitOfWork",
    ):
        assert hasattr(ports, name)
    assert CalendarStore is not None
    assert FeedFetcher is not None
    assert SyncCoordinator is not None
    assert Clock is not None
    assert AuditLogger is not None
    assert Cipher is not None
    assert RateLimiterPort is not None
    assert SourceRepository is not None
    assert MappingRepository is not None
    assert SyncStateRepository is not None
    assert TokenRepository is not None
    assert SecretStore is not None
    assert UnitOfWork is not None


def test_legacy_reexport_shims() -> None:
    from app.services.agent import expand as agent_expand
    from app.sync import ical, ical_time, ical_types, time_values

    assert ical.CalendarEvent is not None
    assert ical_types.Mark is not None
    assert ical_time._zone is not None
    assert time_values.timezone_ok("UTC") is True
    assert agent_expand.expand_object is not None
