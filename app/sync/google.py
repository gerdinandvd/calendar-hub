"""Google import. The read path arrives in a later phase."""


class NotAvailable(Exception):
    """Manual sync can start, but this phase does not import Google."""

    def __init__(self) -> None:
        self.message = "Google import is not available yet."
        super().__init__(self.message)


def import_source() -> None:
    raise NotAvailable()
