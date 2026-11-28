"""In-memory fakes shared by the test suite."""

import threading

from app.radicale.client import RadicaleError


class FakeRadicale:
    """In-memory CalDAV stand-in used by HTTP and sync tests."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.events: dict[str, bytes] = {}
        self.fail_create = False
        self.fail_put = False
        self.fail_after: int | None = None
        self.block_put: threading.Event | None = None
        self.put_started = threading.Event()
        self._lock = threading.Lock()

    def create_collection(self, path: str, display_name: str, color: str) -> None:
        self.calls.append(("create", path, display_name, color))
        if self.fail_create:
            raise RadicaleError("The calendar could not be saved.")

    def update_properties(self, path: str, display_name: str, color: str) -> None:
        self.calls.append(("proppatch", path, display_name, color))

    def delete_collection(self, path: str) -> None:
        self.calls.append(("delete", path))
        self.events = {
            key: value for key, value in self.events.items() if not key.startswith(path)
        }

    def put_event(self, path: str, body: bytes, *, fresh: bool = False) -> None:
        with self._lock:
            self.calls.append(("put", path, body))
            if self.block_put is not None:
                self.put_started.set()
                self.block_put.wait(5)
            puts = [call for call in self.calls if call[0] == "put"]
            if self.fail_put or (
                self.fail_after is not None and len(puts) > self.fail_after
            ):
                raise RadicaleError("The calendar could not be saved.")
            self.events[path] = body

    def delete_event(self, path: str) -> None:
        self.calls.append(("delete-event", path))
        self.events.pop(path, None)

    def get_event(self, path: str) -> bytes | None:
        self.calls.append(("get", path))
        return self.events.get(path)

    def count_events(self, path: str) -> int:
        return len(self.list_event_hrefs(path))

    def list_event_hrefs(self, path: str) -> list[str]:
        prefix = path if path.endswith("/") else f"{path}/"
        return sorted(
            key
            for key in self.events
            if key.startswith(prefix) and key.lower().endswith(".ics")
        )

    def reachable(self) -> bool:
        return True
