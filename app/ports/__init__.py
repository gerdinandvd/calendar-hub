"""Protocol interfaces only. Import app.domain and stdlib; nothing else from app.*."""

from app.ports.calendar_store import CalendarStore
from app.ports.feed import FeedFetcher
from app.ports.repositories import (
    MappingRepository,
    SecretStore,
    SourceRepository,
    SyncStateRepository,
    TokenRepository,
    UnitOfWork,
)
from app.ports.sync import SyncCoordinator
from app.ports.system import AuditLogger, Cipher, Clock, RateLimiterPort

__all__ = [
    "AuditLogger",
    "CalendarStore",
    "Cipher",
    "Clock",
    "FeedFetcher",
    "MappingRepository",
    "RateLimiterPort",
    "SecretStore",
    "SourceRepository",
    "SyncCoordinator",
    "SyncStateRepository",
    "TokenRepository",
    "UnitOfWork",
]
