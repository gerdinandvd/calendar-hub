"""Control-plane tables. Events stay in Radicale, not in SQLite."""

from sqlalchemy import Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Source(Base):
    """One calendar source. Kind-specific columns stay empty until a later phase."""

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
