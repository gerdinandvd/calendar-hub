"""Polls Google and ICS sources."""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Source, SyncState
from app.radicale.client import RadicaleClient
from app.sync.runner import (
    SHUTDOWN_SECONDS,
    SlotBusy,
    SyncRejected,
    start_sync,
    stop_runs,
)

TICK_SECONDS = 10


class Scheduler:
    """One asyncio loop in the process. It does not create sources or change intervals."""

    def __init__(
        self,
        engine: Engine,
        radicale: RadicaleClient,
        interval: float = TICK_SECONDS,
    ) -> None:
        self.engine = engine
        self.radicale = radicale
        self.interval = interval
        self._loop_stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def pause(self) -> None:
        self._loop_stop.set()

    def tick(self, now: datetime | None = None) -> list[str]:
        moment = now if now is not None else datetime.now(UTC)
        started: list[str] = []
        for source_id in due_source_ids(self.engine, moment):
            try:
                start_sync(self.engine, self.radicale, source_id, actor="scheduler")
            except (SlotBusy, SyncRejected):
                continue
            started.append(source_id)
        return started

    async def shutdown(self) -> None:
        self._loop_stop.set()
        if self._task is not None:
            await self._task
        await asyncio.to_thread(stop_runs, self.engine, SHUTDOWN_SECONDS)

    async def _loop(self) -> None:
        while not self._loop_stop.is_set():
            try:
                await asyncio.wait_for(self._loop_stop.wait(), timeout=self.interval)
            except TimeoutError:
                self.tick()


def due_source_ids(engine: Engine, now: datetime) -> list[str]:
    moment = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
    chosen: list[str] = []
    with Session(engine) as session:
        rows = session.scalars(
            select(Source).order_by(Source.created_at, Source.id)
        ).all()
        for row in rows:
            if (
                row.kind not in {"google", "ics"}
                or row.enabled != 1
                or not row.poll_seconds
            ):
                continue
            state = session.get(SyncState, row.id)
            if state is None or state.status in {"running", "na"}:
                continue
            if state.last_success_at:
                elapsed = _elapsed(state.last_success_at, moment)
                if elapsed is not None and elapsed < row.poll_seconds:
                    continue
            chosen.append(row.id)
    return chosen


def _elapsed(stamp: str, now: datetime) -> float | None:
    try:
        last = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (now - last).total_seconds()
