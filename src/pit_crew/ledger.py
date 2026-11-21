"""Where records live. The only state shared between lanes."""

from typing import Protocol, TypeVar

from pit_crew.contracts import Record

R = TypeVar("R", bound=Record)


class Ledger(Protocol):
    def add(self, record: Record) -> None: ...

    def get(self, kind: type[R], record_id: str) -> R: ...

    def all(self, kind: type[R]) -> list[R]: ...


class InMemoryLedger:
    """For tests and local runs. A database-backed ledger replaces it in production."""

    def __init__(self) -> None:
        self._records: dict[tuple[type, str], Record] = {}

    def add(self, record: Record) -> None:
        self._records[(type(record), record.id)] = record

    def get(self, kind: type[R], record_id: str) -> R:
        return self._records[(kind, record_id)]  # type: ignore[return-value]

    def all(self, kind: type[R]) -> list[R]:
        return [r for (k, _), r in self._records.items() if k is kind]  # type: ignore[misc]
