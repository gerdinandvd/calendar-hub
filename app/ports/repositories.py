"""Persistence ports for control-plane tables."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from app.domain.types import (
    AgentTokenRecord,
    EventMappingRecord,
    SourceRecord,
    SyncStateRecord,
)


class SourceRepository(Protocol):
    def get(self, source_id: str) -> SourceRecord | None: ...

    def get_by_name(self, name: str) -> SourceRecord | None: ...

    def list_all(self) -> list[SourceRecord]: ...

    def add(self, source: SourceRecord) -> None: ...

    def save(self, source: SourceRecord) -> None: ...

    def delete(self, source_id: str) -> None: ...

    def count(self) -> int: ...


class MappingRepository(Protocol):
    def get_by_external(
        self, source_id: str, external_id: str
    ) -> EventMappingRecord | None: ...

    def get_by_uid(
        self, source_id: str, ical_uid: str
    ) -> EventMappingRecord | None: ...

    def list_live(self, source_id: str) -> list[EventMappingRecord]: ...

    def remember(
        self,
        source_id: str,
        external_id: str,
        ical_uid: str,
        version: str,
        when: str,
        *,
        mapping_id: str | None = None,
    ) -> str: ...

    def remember_delete(self, mapping_id: str, when: str) -> None: ...

    def remember_batch(self, mappings: list[EventMappingRecord]) -> None: ...


class SyncStateRepository(Protocol):
    def get(self, source_id: str) -> SyncStateRecord | None: ...

    def list_all(self) -> list[SyncStateRecord]: ...

    def add(self, state: SyncStateRecord) -> None: ...

    def save(self, state: SyncStateRecord) -> None: ...


class TokenRepository(Protocol):
    def get(self, token_id: str) -> AgentTokenRecord | None: ...

    def get_by_hash(self, token_hash: str) -> AgentTokenRecord | None: ...

    def list_all(self) -> list[AgentTokenRecord]: ...

    def add(self, token: AgentTokenRecord) -> None: ...

    def save(self, token: AgentTokenRecord) -> None: ...


class SecretStore(Protocol):
    def get_ics_authorization(self, source_id: str) -> str | None: ...

    def set_ics_authorization(self, source_id: str, ciphertext: str | None) -> None: ...

    def delete(self, source_id: str) -> None: ...


class UnitOfWork(Protocol):
    """Transactional boundary over repositories."""

    sources: SourceRepository
    mappings: MappingRepository
    sync_states: SyncStateRepository
    tokens: TokenRepository
    secrets: SecretStore

    def begin(self) -> AbstractContextManager[UnitOfWork]: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
