"""Pydantic request bodies for agent create/patch."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.agent.types import SUMMARY_MAX_LEN, TEXT_MAX_LEN


class AgentCreateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX_LEN)
    start: str
    end: str
    all_day: bool = False
    timezone: str | None = None
    description: str | None = None
    location: str | None = None
    rrule: str | None = None
    status: str | None = None

    @field_validator("calendar")
    @classmethod
    def _calendar_nonempty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Enter a calendar.")
        return cleaned

    @field_validator("summary")
    @classmethod
    def _summary_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a summary.")
        return value

    @field_validator("description", "location")
    @classmethod
    def _text_len(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if len(value) > TEXT_MAX_LEN:
            raise ValueError("The input is invalid.")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone_type(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Enter a valid time zone.")
        return value


class AgentPatchEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str | None = None
    start: str | None = None
    end: str | None = None
    all_day: bool | None = None
    timezone: str | None = None
    description: str | None = None
    location: str | None = None
    rrule: str | None = None
    status: str | None = None

    @field_validator("summary")
    @classmethod
    def _summary(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Enter a summary.")
        if len(value) > SUMMARY_MAX_LEN:
            raise ValueError("The input is invalid.")
        return value

    @field_validator("description", "location")
    @classmethod
    def _text_len(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("The input is invalid.")
        if len(value) > TEXT_MAX_LEN:
            raise ValueError("The input is invalid.")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone_type(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Enter a valid time zone.")
        return value
