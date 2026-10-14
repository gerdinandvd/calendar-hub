"""Request bodies for source routes."""

from pydantic import BaseModel, ConfigDict


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    timezone: str | None = None
    enabled: int | None = None
    kind: str = "private"
    ics_url: str | None = None
    ics_allow_http: int | None = None
    poll_seconds: int | None = None
    ics_authorization_header: str | None = None


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    timezone: str | None = None
    enabled: int | None = None
    ics_url: str | None = None
    ics_allow_http: int | None = None
    poll_seconds: int | None = None
    ics_authorization_header: str | None = None
    clear_ics_authorization: int | None = None
