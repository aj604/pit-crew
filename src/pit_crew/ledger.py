"""Where records live. The only state shared between lanes."""

from typing import Protocol, TypeVar

from pit_crew.contracts import Record

R = TypeVar("R", bound=Record)


class Ledger(Protocol):
    """Protocol for persisting and retrieving lane records."""

    def add(self, record: Record) -> None:
        """Persist a record in the ledger."""
        ...

    def get(self, kind: type[R], record_id: str) -> R:
        """Fetch a single record by type and ID."""
        ...

    def all(self, kind: type[R]) -> list[R]:
        """Return all records of the given type."""
        ...


class InMemoryLedger:
    """For tests and local runs. A database-backed ledger replaces it in production."""

    def __init__(self) -> None:
        """Initialize an empty in-memory store."""
        self._records: dict[tuple[type, str], Record] = {}

    def add(self, record: Record) -> None:
        """Store a record keyed by its concrete type and ID."""
        self._records[(type(record), record.id)] = record

    def get(self, kind: type[R], record_id: str) -> R:
        """Return the record matching ``kind`` and ``record_id``."""
        return self._records[(kind, record_id)]  # type: ignore[return-value]

    def all(self, kind: type[R]) -> list[R]:
        """Return all stored records of ``kind``."""
        return [r for (k, _), r in self._records.items() if k is kind]  # type: ignore[misc]
