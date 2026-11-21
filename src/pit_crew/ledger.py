"""Where records live. The only state shared between lanes."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol, override

from pit_crew.contracts import BugCase, Candidate, Evidence, Hypothesis, Record, ReplayPlan, Verdict


class Ledger(Protocol):
    """Protocol for persisting and retrieving lane records."""

    def add(self, record: Record) -> None:
        """Persist a record in the ledger."""
        ...

    def get[R: Record](self, kind: type[R], record_id: str) -> R:
        """Fetch a single record by type and ID."""
        ...

    def all[R: Record](self, kind: type[R]) -> list[R]:
        """Return all records of the given type."""
        ...

    def hypotheses_for_repo(self, repo: str) -> list[Hypothesis]:
        """Return every ``Hypothesis`` recorded for ``repo``."""
        ...

    def prior_attempts(self, repo: str) -> list[dict[str, Any]]:
        """Return prior hypotheses, candidates, and verdicts for ``repo``."""
        ...

    def latest_kept_candidate(self, repo: str) -> Candidate | None:
        """Return the most recent kept ``Candidate`` for ``repo``, if any."""
        ...

    def records_for_repo(self, repo: str) -> dict[str, list[dict[str, Any]]]:
        """Return all ledger records scoped to ``repo`` grouped by record type."""
        ...


def _normalize_repo(repo: str) -> str:
    """Normalize a repository identifier for case-insensitive matching."""
    return repo.strip().lower()


class _QueryMixin:
    """Shared read queries built on ``all()`` so every Ledger backend behaves identically."""

    def all[R: Record](self, kind: type[R]) -> list[R]:
        raise NotImplementedError

    def hypotheses_for_repo(self, repo: str) -> list[Hypothesis]:
        """Return every ``Hypothesis`` recorded for ``repo``."""
        repo_key = _normalize_repo(repo)
        return [h for h in self.all(Hypothesis) if _normalize_repo(h.repo) == repo_key]

    def prior_attempts(self, repo: str) -> list[dict[str, Any]]:
        """History of hypotheses, candidates and verdicts for ``repo`` (oldest first)."""
        repo_key = _normalize_repo(repo)
        hypotheses = {h.id: h for h in self.hypotheses_for_repo(repo)}
        verdicts_by_candidate = {v.candidate_id: v for v in self.all(Verdict)}

        attempts: list[dict[str, Any]] = []
        seen_hypotheses: set[str] = set()
        for cand in self.all(Candidate):
            hyp = hypotheses.get(cand.hypothesis_id)
            if hyp is None and _normalize_repo(cand.repo) != repo_key:
                continue
            seen_hypotheses.add(cand.hypothesis_id)
            verdict = verdicts_by_candidate.get(cand.id)
            attempts.append(_attempt_dict(hyp, cand, verdict))

        attempts.extend(
            _unattempted_hypothesis_dict(hyp) for hyp_id, hyp in hypotheses.items() if hyp_id not in seen_hypotheses
        )
        return attempts

    def latest_kept_candidate(self, repo: str) -> Candidate | None:
        """Most recent kept Candidate for ``repo``, used to advance ``base_sha`` when compounding."""
        repo_key = _normalize_repo(repo)
        hypotheses = {h.id for h in self.hypotheses_for_repo(repo)}
        kept_ids = {v.candidate_id for v in self.all(Verdict) if v.keep}
        latest: Candidate | None = None
        for cand in self.all(Candidate):
            if cand.id in kept_ids and (cand.hypothesis_id in hypotheses or _normalize_repo(cand.repo) == repo_key):
                latest = cand
        return latest

    def records_for_repo(self, repo: str) -> dict[str, list[dict[str, Any]]]:
        """Return all ledger records scoped to ``repo`` grouped by record type."""
        repo_key = _normalize_repo(repo)
        hypotheses = self.hypotheses_for_repo(repo)
        hypothesis_ids = {h.id for h in hypotheses}
        candidates = [
            c for c in self.all(Candidate) if c.hypothesis_id in hypothesis_ids or _normalize_repo(c.repo) == repo_key
        ]
        candidate_ids = {c.id for c in candidates}
        replay_plans = [p for p in self.all(ReplayPlan) if p.candidate_id in candidate_ids]
        evidence = [e for e in self.all(Evidence) if e.candidate_id in candidate_ids]
        verdicts = [v for v in self.all(Verdict) if v.candidate_id in candidate_ids]
        return {
            "hypotheses": [asdict(h) for h in hypotheses],
            "candidates": [asdict(c) for c in candidates],
            "replay_plans": [asdict(p) for p in replay_plans],
            "evidence": [asdict(e) for e in evidence],
            "verdicts": [asdict(v) for v in verdicts],
        }


def _attempt_dict(hyp: Hypothesis | None, cand: Candidate, verdict: Verdict | None) -> dict[str, Any]:
    return {
        "hypothesis_id": cand.hypothesis_id,
        "skill": hyp.skill if hyp else "",
        "description": hyp.description if hyp else "",
        "candidate_id": cand.id,
        "branch": cand.branch,
        "base_sha": cand.base_sha or (hyp.base_sha if hyp else ""),
        "head_sha": cand.head_sha,
        "summary": cand.summary,
        "changed_files": list(cand.changed_files),
        "rated": verdict is not None,
        "keep": verdict.keep if verdict else None,
        "speedup": verdict.speedup if verdict else None,
        "p_value": verdict.p_value if verdict else None,
        "median_seconds": verdict.median_seconds if verdict else None,
        "p95_seconds": verdict.p95_seconds if verdict else None,
        "sla_met": verdict.sla_met if verdict else None,
        "reason": verdict.reason if verdict else None,
    }


def _unattempted_hypothesis_dict(hyp: Hypothesis) -> dict[str, Any]:
    return {
        "hypothesis_id": hyp.id,
        "skill": hyp.skill,
        "description": hyp.description,
        "candidate_id": None,
        "branch": None,
        "base_sha": hyp.base_sha,
        "head_sha": None,
        "summary": None,
        "changed_files": [],
        "rated": False,
        "keep": None,
        "speedup": None,
        "p_value": None,
        "median_seconds": None,
        "p95_seconds": None,
        "sla_met": None,
        "reason": None,
    }


class InMemoryLedger(_QueryMixin):
    """For tests and local runs. A database-backed ledger replaces it in production."""

    def __init__(self) -> None:
        """Initialize an empty in-memory store."""
        self._records: dict[tuple[type, str], Record] = {}

    def add(self, record: Record) -> None:
        """Store a record keyed by its concrete type and ID."""
        self._records[(type(record), record.id)] = record

    def get[R: Record](self, kind: type[R], record_id: str) -> R:
        """Return the record matching ``kind`` and ``record_id``."""
        return self._records[(kind, record_id)]  # type: ignore[return-value]

    @override
    def all[R: Record](self, kind: type[R]) -> list[R]:
        """Return all stored records of ``kind``."""
        return [r for (k, _), r in self._records.items() if k is kind]  # type: ignore[misc]


class SqliteLedger(_QueryMixin):
    """WAL-mode SQLite ledger shared safely between FastAPI webhooks, Dispatcher, and Pit Wall UI."""

    def __init__(self, path: str | Path) -> None:
        """Open or create the SQLite ledger database at ``path`` in WAL mode."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(kind, record_id)
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection inside a transaction, then close it (``with conn`` alone never closes)."""
        conn = sqlite3.connect(self.path, timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            with conn:
                yield conn
        finally:
            conn.close()

    def add(self, record: Record) -> None:
        """Persist or update ``record`` as JSON in SQLite."""
        kind_name = type(record).__name__
        payload = json.dumps(asdict(record), sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO records (kind, record_id, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(kind, record_id) DO UPDATE SET payload = excluded.payload
                """,
                (kind_name, record.id, payload),
            )

    def get[R: Record](self, kind: type[R], record_id: str) -> R:
        """Fetch the ``kind`` record with ``record_id`` from SQLite or raise ``KeyError``."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM records WHERE kind = ? AND record_id = ?",
                (kind.__name__, record_id),
            ).fetchone()
        if row is None:
            raise KeyError((kind, record_id))
        return _deserialize(kind, json.loads(row[0]))

    @override
    def all[R: Record](self, kind: type[R]) -> list[R]:
        """List all persisted records of ``kind`` in insertion order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM records WHERE kind = ? ORDER BY seq ASC",
                (kind.__name__,),
            ).fetchall()
        return [_deserialize(kind, json.loads(row[0])) for row in rows]


def _deserialize[R: Record](kind: type[R], data: dict[str, Any]) -> R:
    if kind is Candidate:
        data["changed_files"] = tuple(data.get("changed_files", ()))
    elif kind is ReplayPlan:
        data["bug_shas"] = tuple(data.get("bug_shas", ()))
        data["cases"] = tuple(_deserialize_bug_case(case) for case in data.get("cases", ()))
    elif kind is Evidence:
        data["baseline_seconds"] = tuple(data["baseline_seconds"])
        data["candidate_seconds"] = tuple(data["candidate_seconds"])
        data["run_urls"] = tuple(data.get("run_urls", ()))
        data["bug_outcomes"] = tuple(data.get("bug_outcomes", ()))
    return kind(**data)


def _deserialize_bug_case(data: dict[str, Any]) -> BugCase:
    case = dict(data)
    raw_checks = case.get("expected_checks")
    if isinstance(raw_checks, list):
        case["expected_checks"] = tuple(raw_checks)
    return BugCase(**case)
