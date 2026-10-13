"""Control-plane tables. Events stay in Radicale, not in SQLite."""

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Source(Base):
    """One calendar source. Kind-specific columns stay empty until that kind is built."""

    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    color: Mapped[str] = mapped_column(String, nullable=False)
    collection_path: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    enabled: Mapped[int] = mapped_column(Integer, nullable=False)
    poll_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    ics_url: Mapped[str | None] = mapped_column(String, nullable=True)
    ics_allow_http: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    google_calendar_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class SourceSecret(Base):
    """Ciphertext for one source. Plaintext never goes into this table."""

    __tablename__ = "source_secrets"

    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    webhook_secret: Mapped[str | None] = mapped_column(String, nullable=True)
    oauth_refresh_token: Mapped[str | None] = mapped_column(String, nullable=True)
    oauth_access_token: Mapped[str | None] = mapped_column(String, nullable=True)
    oauth_expiry: Mapped[str | None] = mapped_column(String, nullable=True)
    ics_authorization_header: Mapped[str | None] = mapped_column(String, nullable=True)


class EventMapping(Base):
    """Links an external id to one stable UID in Radicale."""

    __tablename__ = "event_mappings"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_mapping_source_external"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    ical_uid: Mapped[str] = mapped_column(String, nullable=False)
    external_version: Mapped[str | None] = mapped_column(String, nullable=True)
    last_synced_at: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)


class SyncState(Base):
    """One sync status row per source."""

    __tablename__ = "sync_states"

    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    cursor: Mapped[str | None] = mapped_column(String, nullable=True)
    last_started_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_success_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    events_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_upserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    events_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
