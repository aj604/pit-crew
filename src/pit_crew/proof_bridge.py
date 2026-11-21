"""Deterministic pre-Proof coordinator, multimodal artifact collection, and PR proof publishing.

Before the Proof lane agent runs, ``ProofBridge`` diffs original workflow files at ``base_sha``
against ``Candidate.files`` to identify ``touched_checks``, mines deterministic ``red_commits``
from GitHub Actions job runs, computes ``required_replays`` and ``uncovered_checks``, optionally
mounts historical failure screenshots under ``/workspace/.pit-crew/artifacts/``, and embeds
that context into the Proof lane task prompt.

After replay runs complete, ``ProofBridge.verify_replay_runs()`` collects typed multimodal
``ArtifactBundle``s (merging failing shard ZIPs + failed job logs), verifies failure equivalence
with ``gemini-4-carbon`` under fail-closed visual-downgrade and ``expected_checks`` guards, and
``open_kept_pr()`` commits paired PNG previews alongside ``.github/pit-crew/proof-card.svg``.
"""

import base64
import inspect
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial
from pathlib import PurePosixPath
from typing import Any, Protocol, runtime_checkable

from pit_crew.contracts import Candidate, Evidence, Verdict
from pit_crew.github import PullRequestClient, RunArtifact, RunArtifactsClient
from pit_crew.lanes.proof.artifacts import (
    MAX_SHARD_ARTIFACTS,
    MAX_ZIP_DOWNLOAD_BYTES,
    ArtifactBundle,
    MediaPart,
    UnsafeArchiveError,
    bundle_from_raw,
    extract_zip_artifact,
    is_png,
    score_artifact_name,
    select_bundle_parts,
)
from pit_crew.lanes.proof.card import pr_body, render_card
from pit_crew.lanes.proof.checks import Check, FileChange, touched_checks
from pit_crew.lanes.proof.miner import JobRun, RedCommit, flaky_checks, red_commits
from pit_crew.lanes.proof.replay_workflow import (
    RECORD_SCRIPT_PATH,
    REPLAY_ARTIFACT_PREFIX,
    REPLAY_META_FILENAME,
    REPLAY_RECORD_ENV,
    ReplayArtifactError,
    build_replay_overlay,
    extract_replay_video,
    instrument_workflow,
    resolve_workflow_check_aliases,
)
from pit_crew.lanes.proof.tools import MAX_REPLAYS, pick_replays, uncovered_checks
from pit_crew.lanes.proof.verify import (
    AskModel,
    FailureMatch,
    Replay,
    compare_bundles,
    compare_failures,
    expected_checks_matched,
    tally,
    verify_replays,
)
from pit_crew.ledger import Ledger

__all__ = [
    "HISTORICAL_MAX_SCREENSHOTS",
    "HISTORICAL_MAX_SCREENSHOT_BYTES",
    "HISTORICAL_MAX_TOTAL_BYTES",
    "PROOF_ARTIFACTS_DIR",
    "PROOF_CARD_PATH",
    "PR_MAX_PREVIEW_BYTES",
    "PR_MAX_PREVIEW_PAIRS",
    "RECORD_SCRIPT_PATH",
    "REPLAY_ARTIFACT_PREFIX",
    "REPLAY_META_FILENAME",
    "REPLAY_RECORD_ENV",
    "SANDBOX_ARTIFACTS_DIR",
    "LaneRunner",
    "MultimodalReplayVerifier",
    "OpenedPullRequest",
    "ProofBridge",
    "ProofBridgeError",
    "ProofContext",
    "ProofHistoryClient",
    "ProofPRConfig",
    "ReplayArtifactError",
    "ReplayFailureOutputClient",
    "ReplayRunPair",
    "ReplayVerification",
    "build_replay_overlay",
    "extract_replay_video",
    "instrument_workflow",
    "resolve_workflow_check_aliases",
    "verify_replay_run_pairs",
]

logger = logging.getLogger(__name__)

PROOF_CARD_PATH = ".github/pit-crew/proof-card.svg"
PROOF_ARTIFACTS_DIR = ".github/pit-crew/proof-artifacts"
SANDBOX_ARTIFACTS_DIR = "/workspace/.pit-crew/artifacts"

HISTORICAL_MAX_SCREENSHOTS = 3
HISTORICAL_MAX_SCREENSHOT_BYTES = 512 * 1024  # 512 KiB per historical screenshot
HISTORICAL_MAX_TOTAL_BYTES = 1536 * 1024  # 1.5 MiB total mounted historical screenshots

PR_MAX_PREVIEW_PAIRS = 3
PR_MAX_PREVIEW_BYTES = 500 * 1024  # 500 KiB per committed PR failure PNG

_IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ProofBridgeError(ValueError):
    """Raised when ``ProofBridge`` cannot prepare context or invoke the Proof lane."""

    @classmethod
    def missing_repo(cls, candidate_id: str) -> "ProofBridgeError":
        """Build error when neither ``candidate.repo`` nor ``repo`` is provided."""
        return cls(f"Candidate {candidate_id} has no repo; pass repo explicitly or set Candidate.repo")

    @classmethod
    def missing_runner(cls) -> "ProofBridgeError":
        """Build error when ``run()`` is called without a configured ``run_lane`` callable."""
        return cls("ProofBridge.run() requires a run_lane callable")

    @classmethod
    def missing_verifier(cls) -> "ProofBridgeError":
        """Build error when ``verify_replay_runs()`` is called without a multimodal ``ask`` callable."""
        return cls("ProofBridge.verify_replay_runs() requires an ask callable (e.g. managed_agent.ask_multimodal)")

    @classmethod
    def missing_pr_client(cls) -> "ProofBridgeError":
        """Build error when ``open_kept_pr()`` is called without a ``PullRequestClient``."""
        return cls("ProofBridge.open_kept_pr() requires a PullRequestClient (github or pr_client)")


@runtime_checkable
class ReplayFailureOutputClient(Protocol):
    """Loads recorded replay failure output bytes for a GitHub Actions workflow run."""

    def get_run_failure_output(self, repo: str, run_id: str) -> bytes:
        """Return failed job logs or recorded artifact bytes for workflow run ``run_id`` in ``repo``."""
        ...


class ProofHistoryClient(ReplayFailureOutputClient, Protocol):
    """Reads base files and recent workflow job runs for Proof pre-pass mining."""

    def get_file_content(self, repo: str, ref: str, path: str) -> str | None:
        """Return UTF-8 file content at ``ref`` in ``repo``, or ``None`` if absent."""
        ...

    def list_workflow_job_runs(self, repo: str) -> Sequence[JobRun]:
        """Return recent GitHub Actions job run attempts for ``repo``."""
        ...


class LaneRunner(Protocol):
    """Callable that invokes a lane with a task prompt (e.g. ``managed_agent.run_lane``)."""

    def __call__(self, lane: str, task: str, *, repo: str = "", ledger: Ledger | None = None) -> object:
        """Execute ``lane`` with ``task`` and return the lane interaction/result."""
        ...


@dataclass(frozen=True)
class ReplayRunPair:
    """One bug replay pair measured on baseline and candidate workflow runs."""

    sha: str
    check: str
    baseline_conclusion: str
    candidate_conclusion: str
    baseline_run_id: str
    candidate_run_id: str
    baseline_run_url: str = ""
    candidate_run_url: str = ""
    expected_checks: tuple[str, ...] = ()
    baseline_failed_checks: tuple[str, ...] = ()
    candidate_failed_checks: tuple[str, ...] = ()
    check_aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayVerification:
    """Verified replay outcomes ready to populate ``Evidence`` and PR visual previews."""

    replays: tuple[Replay, ...]
    bugs_total: int
    bugs_caught: int
    bug_outcomes: tuple[dict[str, object], ...]
    preview_files: tuple[tuple[str, bytes], ...] = ()

    def to_evidence(
        self,
        candidate_id: str,
        baseline_seconds: tuple[float, ...],
        candidate_seconds: tuple[float, ...],
        *,
        run_urls: tuple[str, ...] = (),
    ) -> Evidence:
        """Build a ``contracts.Evidence`` record from these verified replay outcomes."""
        return Evidence(
            candidate_id=candidate_id,
            baseline_seconds=baseline_seconds,
            candidate_seconds=candidate_seconds,
            bugs_total=self.bugs_total,
            bugs_caught=self.bugs_caught,
            run_urls=run_urls,
            bug_outcomes=self.bug_outcomes,
        )


@dataclass(frozen=True)
class ProofPRConfig:
    """Configuration for Proof Card attachment and PR creation on kept candidates."""

    auto_open_pr: bool = False
    base_branch: str = "main"
    repo: str = ""


@dataclass(frozen=True)
class OpenedPullRequest:
    """Metadata for a pull request opened with a committed SVG Proof Card and visual previews."""

    url: str
    card_path: str
    card_sha: str
    card_url: str
    title: str
    body: str
    artifact_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProofContext:
    """Deterministic pre-Proof analysis computed before invoking the Proof lane agent."""

    candidate: Candidate
    repo: str
    base_sha: str
    touched_checks: frozenset[Check] | None
    red_commits: tuple[RedCommit, ...]
    flaky_checks: tuple[tuple[str, str], ...]
    required_replays: tuple[str, ...]
    uncovered_checks: frozenset[Check]
    mounted_artifacts: tuple[str, ...] = ()
    extra_sources: tuple[dict[str, str], ...] = ()


def verify_replay_run_pairs(
    github: ReplayFailureOutputClient,
    repo: str,
    runs: Iterable[ReplayRunPair],
    *,
    ask: AskModel,
) -> ReplayVerification:
    """Verify replay pairs via ``verify_replays``, ``tally``, and ``Replay.as_outcome``.

    Resolves expected checks against completed job outcomes on both baseline and candidate runs
    when available, downloads only check-scoped recording artifacts (verifying ``replay-meta.json``
    when present), extracts MP4 bytes safely, and delegates multimodal equivalence checking to
    ``compare_failures(..., ask=ask)``. Any check mismatch, ambiguous/missing video, corrupt
    archive, or model error fails closed and does not increment ``bugs_caught``.
    """
    compare = partial(compare_failures, ask=ask)
    verified_tuple = tuple(_verify_replay_run_pair(github, repo, pair, compare=compare) for pair in runs)
    bugs_total, bugs_caught = tally(verified_tuple)
    return ReplayVerification(
        replays=verified_tuple,
        bugs_total=bugs_total,
        bugs_caught=bugs_caught,
        bug_outcomes=tuple(replay.as_outcome() for replay in verified_tuple),
    )


def _verify_replay_run_pair(
    github: ReplayFailureOutputClient,
    repo: str,
    pair: ReplayRunPair,
    *,
    compare: Callable[[bytes, bytes, str], FailureMatch],
) -> Replay:
    expected = _effective_expected_checks(pair)
    raw = Replay(
        sha=pair.sha,
        check=pair.check,
        baseline_failed=pair.baseline_conclusion == "failure",
        candidate_failed=pair.candidate_conclusion == "failure",
        baseline_video=pair.baseline_run_id,
        candidate_video=pair.candidate_run_id,
        expected_checks=pair.expected_checks,
        candidate_failed_checks=pair.candidate_failed_checks,
    )
    if not (raw.baseline_failed and raw.candidate_failed):
        return raw

    guard_result = _check_pair_job_identities(github, repo, pair, raw, expected)
    if guard_result is not None:
        return guard_result

    def load_side(run_id: str) -> bytes:
        raw_bytes = _load_failure_output(
            github,
            repo,
            run_id,
            check=pair.check if expected else "",
            expected_checks=expected,
            check_aliases=pair.check_aliases,
        )
        return extract_replay_video(raw_bytes, expected_checks=expected, check_aliases=pair.check_aliases)

    return verify_replays([raw], load=load_side, compare=compare)[0]


def _effective_expected_checks(pair: ReplayRunPair) -> tuple[str, ...]:
    cleaned = tuple(c.strip() for c in pair.expected_checks if c.strip())
    if cleaned:
        return cleaned
    check = pair.check.strip()
    if check and not check.lower().endswith((".yml", ".yaml")):
        return (check,)
    return ()


def _check_pair_job_identities(
    github: object,
    repo: str,
    pair: ReplayRunPair,
    raw: Replay,
    expected: tuple[str, ...],
) -> Replay | None:
    if not expected:
        return None
    expected_with_aliases = tuple(dict.fromkeys((*expected, *pair.check_aliases)))

    base_failed = pair.baseline_failed_checks or _fetch_failed_job_names_from(github, repo, pair.baseline_run_id)
    if base_failed and not expected_checks_matched(expected_with_aliases, base_failed):
        expected_list = ", ".join(expected)
        actual_list = ", ".join(base_failed)
        return replace(
            raw,
            note=f"not verified: expected check(s) ({expected_list}) did not fail on baseline (failed: {actual_list})",
        )

    cand_failed = pair.candidate_failed_checks or _fetch_failed_job_names_from(github, repo, pair.candidate_run_id)
    if cand_failed and not expected_checks_matched(expected_with_aliases, cand_failed):
        expected_list = ", ".join(expected)
        actual_list = ", ".join(cand_failed)
        return replace(
            raw,
            candidate_failed_checks=cand_failed,
            match=FailureMatch(
                same=False,
                baseline_failure=f"expected failing check(s): {expected_list}",
                candidate_failure=f"candidate failed on: {actual_list}",
                reason=f"candidate failed on unexpected check(s) ({actual_list}); expected one of {expected_list}",
            ),
        )
    return None


def _load_failure_output(  # noqa: PLR0913
    github: ReplayFailureOutputClient,
    repo: str,
    run_id: str,
    *,
    check: str,
    expected_checks: tuple[str, ...],
    check_aliases: tuple[str, ...],
) -> bytes:
    loader: Any = github.get_run_failure_output
    kwargs: dict[str, Any] = {}
    if _accepts_kwarg(loader, "check"):
        kwargs["check"] = check
    if _accepts_kwarg(loader, "expected_checks"):
        kwargs["expected_checks"] = expected_checks
    if _accepts_kwarg(loader, "check_aliases"):
        kwargs["check_aliases"] = check_aliases
    return bytes(loader(repo, run_id, **kwargs))


def _fetch_failed_job_names_from(github: object, repo: str, run_id: str) -> tuple[str, ...]:
    lister = getattr(github, "list_failed_job_names", None)
    if not callable(lister):
        return ()
    try:
        names = lister(repo, run_id)
    except Exception:  # noqa: BLE001  non-fatal if job list endpoint is unavailable on custom client
        return ()
    if not isinstance(names, Iterable) or isinstance(names, str | bytes):
        return ()
    return tuple(str(name) for name in names if isinstance(name, str) and name.strip())


class MultimodalReplayVerifier:
    """Standalone replay verifier wiring ``ReplayFailureOutputClient`` and multimodal ``ask``."""

    def __init__(self, github: ReplayFailureOutputClient, ask: AskModel) -> None:
        """Create a verifier backed by ``github`` artifact loading and ``ask`` multimodal calls."""
        self._github = github
        self._ask = ask

    def verify_replay_runs(
        self,
        repo: str,
        runs: Iterable[ReplayRunPair],
        *,
        ask: AskModel | None = None,
    ) -> ReplayVerification:
        """Verify replay pairs and return ``ReplayVerification`` ready for ``Evidence``."""
        return verify_replay_run_pairs(self._github, repo, runs, ask=ask if ask is not None else self._ask)


class ProofBridge:
    """Computes deterministic pre-Proof context, collects multimodal bundles, and opens kept PRs."""

    def __init__(
        self,
        github: ProofHistoryClient,
        run_lane: LaneRunner | None = None,
        *,
        replay_limit: int = MAX_REPLAYS,
        ask: AskModel | None = None,
    ) -> None:
        """Create a bridge using ``github`` for history/base files, optional ``run_lane``, and optional ``ask``."""
        self._github = github
        self._run_lane = run_lane
        self._replay_limit = replay_limit
        self._ask = ask

    def prepare_context(
        self,
        candidate: Candidate,
        *,
        repo: str = "",
        base_sha: str = "",
        historical_run_ids: Mapping[str, str | int] | None = None,
    ) -> ProofContext:
        """Compute deterministic Proof context and optionally mount historical failure screenshots."""
        target_repo = repo or candidate.repo
        if not target_repo:
            raise ProofBridgeError.missing_repo(candidate.id)
        target_base = base_sha or candidate.base_sha

        touched = self._compute_touched_checks(candidate, target_repo, target_base)
        job_runs = list(self._github.list_workflow_job_runs(target_repo))
        mined_red = tuple(red_commits(job_runs))
        flaky = tuple(flaky_checks(job_runs))
        required = tuple(pick_replays(mined_red, touched, limit=self._replay_limit))
        uncovered = frozenset(uncovered_checks(mined_red, touched)) if touched is not None else frozenset()

        run_id_by_sha = _historical_run_id_map(job_runs, historical_run_ids)
        mounted_paths, extra_sources = self._mount_historical_screenshots(target_repo, required, run_id_by_sha)

        return ProofContext(
            candidate=candidate,
            repo=target_repo,
            base_sha=target_base,
            touched_checks=None if touched is None else frozenset(touched),
            red_commits=mined_red,
            flaky_checks=flaky,
            required_replays=required,
            uncovered_checks=uncovered,
            mounted_artifacts=mounted_paths,
            extra_sources=extra_sources,
        )

    def build_prompt(self, context: ProofContext, *, extra_instructions: str = "") -> str:
        """Render the Proof lane task prompt with pre-computed deterministic context embedded."""
        candidate = context.candidate
        changed_files = ", ".join(candidate.changed_files) if candidate.changed_files else "(none)"
        sections = [
            f"Build a ReplayPlan for Candidate `{candidate.id}`.",
            "",
            "## Candidate",
            f"- candidate_id: {candidate.id}",
            f"- repo: {context.repo}",
            f"- branch: {candidate.branch}",
            f"- base_sha: {context.base_sha or '(unknown)'}",
            f"- head_sha: {candidate.head_sha or '(unpublished)'}",
            f"- summary: {candidate.summary}",
            f"- changed_files: {changed_files}",
            "",
            "## Touched checks",
            _format_checks(context.touched_checks),
            "",
            "## Required replays",
            _format_shas(context.required_replays),
            "",
            "## Uncovered checks",
            _format_uncovered(context.touched_checks, context.uncovered_checks),
            "",
            "## Red commits (deterministic failures, newest first)",
            _format_red_commits(context.red_commits),
        ]
        if context.mounted_artifacts:
            sections.extend(
                [
                    "",
                    "## Mounted historical failure screenshots",
                    *(f"- {path}" for path in context.mounted_artifacts),
                ]
            )
        if context.flaky_checks:
            sections.extend(
                [
                    "",
                    "## Excluded flaky checks",
                    *(f"- {sha} ({check})" for sha, check in context.flaky_checks),
                ]
            )
        if extra_instructions.strip():
            sections.extend(["", "## Additional instructions", extra_instructions.strip()])
        sections.extend(
            [
                "",
                "## Instructions",
                (
                    f"Keep every required replay above and call `plan_replays(candidate_id={candidate.id!r}, ...)` "
                    "with `bug_shas` and synthetic `mutations` covering each uncovered check."
                ),
            ]
        )
        return "\n".join(sections)

    def run(  # noqa: PLR0913  explicit keyword arguments mirror prepare_context + build_prompt
        self,
        candidate: Candidate,
        *,
        ledger: Ledger | None = None,
        repo: str = "",
        base_sha: str = "",
        extra_instructions: str = "",
        historical_run_ids: Mapping[str, str | int] | None = None,
    ) -> tuple[ProofContext, object]:
        """Prepare pre-Proof context, inject it into the prompt, and invoke the Proof lane."""
        if self._run_lane is None:
            raise ProofBridgeError.missing_runner()
        context = self.prepare_context(
            candidate,
            repo=repo,
            base_sha=base_sha,
            historical_run_ids=historical_run_ids,
        )
        prompt = self.build_prompt(context, extra_instructions=extra_instructions)
        kwargs: dict[str, Any] = {"repo": context.repo, "ledger": ledger}
        if context.extra_sources and _accepts_kwarg(self._run_lane, "extra_sources"):
            kwargs["extra_sources"] = list(context.extra_sources)
        result = self._run_lane("proof", prompt, **kwargs)
        return context, result

    def collect_run_failure_bundle(  # noqa: PLR0913
        self,
        repo: str,
        run_id: str,
        *,
        check: str = "",
        expected_checks: Sequence[str] = (),
        check_aliases: Sequence[str] = (),
        run_url: str = "",
    ) -> ArtifactBundle:
        """Collect a typed, shard-aware ``ArtifactBundle`` for workflow run ``run_id``.

        When ``github`` implements ``RunArtifactsClient``, lists non-expired artifacts,
        ranks and merges up to ``MAX_SHARD_ARTIFACTS`` (4) failing shard ZIPs, and appends
        failed job logs. Falls back to sniffing ``get_run_failure_output()`` for legacy
        clients without artifact endpoints.
        """
        resolved_url = run_url or _default_run_url(repo, run_id)
        if not isinstance(self._github, RunArtifactsClient):
            raw = _load_failure_output(
                self._github,
                repo,
                run_id,
                check=check,
                expected_checks=tuple(expected_checks),
                check_aliases=tuple(check_aliases),
            )
            return bundle_from_raw(raw, run_id=str(run_id), run_url=run_url, fallback_mime="video/mp4")

        parts: list[MediaPart] = []
        artifacts = self._github.list_run_artifacts(repo, run_id)
        ranked = _rank_active_artifacts(
            artifacts,
            check=check,
            check_aliases=tuple(dict.fromkeys((*expected_checks, *check_aliases))),
        )[:MAX_SHARD_ARTIFACTS]
        for artifact in ranked:
            parts.extend(self._extract_run_artifact(repo, artifact, fallback_url=resolved_url))

        log_bytes = _load_failed_job_logs(
            self._github,
            repo,
            run_id,
            check=check or None,
            expected_checks=tuple(expected_checks),
            check_aliases=tuple(check_aliases),
        )
        if log_bytes:
            parts.append(
                MediaPart(
                    name="failed-job.log",
                    mime_type="text/plain",
                    data=log_bytes,
                    modality="log",
                    source_url=resolved_url,
                )
            )
        return select_bundle_parts(parts, run_id=str(run_id), run_url=resolved_url)

    def verify_replay_runs(
        self,
        repo: str,
        runs: Iterable[ReplayRunPair],
        *,
        ask: AskModel | None = None,
    ) -> ReplayVerification:
        """Verify multimodal failure equivalence for replay pairs where both runs failed.

        Collects typed ``ArtifactBundle``s per failing pair, enforces ``expected_checks`` and
        visual-downgrade rules fail-closed, extracts up to 3 paired PNG previews for kept PRs,
        and delegates multimodal judgment to ``compare_bundles(..., ask=ask)``.
        """
        verifier = ask if ask is not None else self._ask
        if verifier is None:
            raise ProofBridgeError.missing_verifier()

        verified: list[Replay] = []
        preview_files: list[tuple[str, bytes]] = []
        preview_pairs_captured = 0

        for idx, pair in enumerate(runs):
            replay, pair_previews = self._verify_single_pair(
                repo,
                pair,
                idx,
                verifier,
                capture_preview=preview_pairs_captured < PR_MAX_PREVIEW_PAIRS,
            )
            verified.append(replay)
            if pair_previews:
                preview_files.extend(pair_previews)
                preview_pairs_captured += 1

        verified_tuple = tuple(verified)
        bugs_total, bugs_caught = tally(verified_tuple)
        return ReplayVerification(
            replays=verified_tuple,
            bugs_total=bugs_total,
            bugs_caught=bugs_caught,
            bug_outcomes=tuple(replay.as_outcome() for replay in verified_tuple),
            preview_files=tuple(preview_files),
        )

    def open_kept_pr(  # noqa: PLR0913  optional preview_files keeps existing signature backwards-compatible
        self,
        candidate: Candidate,
        evidence: Evidence,
        verdict: Verdict,
        replays: Sequence[Replay],
        *,
        config: ProofPRConfig | None = None,
        preview_files: Mapping[str, bytes] | Sequence[tuple[str, bytes]] | None = None,
    ) -> OpenedPullRequest | None:
        """Render and commit the SVG Proof Card (plus capped PNG previews) and open a PR when kept."""
        effective = config or ProofPRConfig()
        if not verdict.keep or not effective.auto_open_pr:
            return None
        target_repo = effective.repo or candidate.repo
        if not target_repo:
            raise ProofBridgeError.missing_repo(candidate.id)
        if not isinstance(self._github, PullRequestClient):
            raise ProofBridgeError.missing_pr_client()

        svg = render_card(candidate.branch, evidence, verdict, replays)
        valid_previews = _filter_pr_preview_files(preview_files)
        commit_payload: dict[str, Any] = {PROOF_CARD_PATH: svg, **valid_previews}
        card_sha = self._commit_proof_files(
            target_repo,
            candidate.branch,
            commit_payload,
            f"pit-crew: add proof card for {candidate.id}",
        )
        raw_base = f"https://raw.githubusercontent.com/{target_repo}/{card_sha}"
        card_url = f"{raw_base}/{PROOF_CARD_PATH}"
        body = pr_body(
            candidate.branch,
            evidence,
            verdict,
            replays,
            card_url=card_url,
            preview_base_url=raw_base if valid_previews else "",
        )
        title = _pr_title(candidate)
        url = self._github.open_pull_request(
            target_repo,
            head=candidate.branch,
            base=effective.base_branch,
            title=title,
            body=body,
        )
        return OpenedPullRequest(
            url=url,
            card_path=PROOF_CARD_PATH,
            card_sha=card_sha,
            card_url=card_url,
            title=title,
            body=body,
            artifact_paths=tuple(sorted(valid_previews)),
        )

    def _verify_single_pair(
        self,
        repo: str,
        pair: ReplayRunPair,
        idx: int,
        verifier: AskModel,
        *,
        capture_preview: bool,
    ) -> tuple[Replay, list[tuple[str, bytes]]]:
        base_replay = Replay(
            sha=pair.sha,
            check=pair.check,
            baseline_failed=pair.baseline_conclusion == "failure",
            candidate_failed=pair.candidate_conclusion == "failure",
            baseline_video=pair.baseline_run_url or pair.baseline_run_id,
            candidate_video=pair.candidate_run_url or pair.candidate_run_id,
            expected_checks=pair.expected_checks,
            candidate_failed_checks=pair.candidate_failed_checks,
        )
        if not (base_replay.baseline_failed and base_replay.candidate_failed):
            return base_replay, []

        effective_expected = _effective_expected_checks(pair)
        expected_with_aliases = tuple(dict.fromkeys((*pair.expected_checks, *pair.check_aliases)))
        if expected_with_aliases:
            base_failed = pair.baseline_failed_checks or self._fetch_failed_checks(
                repo, pair.baseline_run_id, expected_with_aliases
            )
            if base_failed and not expected_checks_matched(expected_with_aliases, base_failed):
                expected_list = ", ".join(pair.expected_checks)
                actual_list = ", ".join(base_failed)
                note = f"not verified: expected check(s) ({expected_list}) did not fail on baseline ({actual_list})"
                return replace(base_replay, note=note), []

        filter_check = pair.check if effective_expected else ""
        try:
            baseline_bundle = self.collect_run_failure_bundle(
                repo,
                pair.baseline_run_id,
                check=filter_check,
                expected_checks=effective_expected,
                check_aliases=pair.check_aliases,
                run_url=pair.baseline_run_url,
            )
            candidate_bundle = self.collect_run_failure_bundle(
                repo,
                pair.candidate_run_id,
                check=filter_check,
                expected_checks=effective_expected,
                check_aliases=pair.check_aliases,
                run_url=pair.candidate_run_url,
            )
            failed_checks = pair.candidate_failed_checks or self._fetch_failed_checks(
                repo, pair.candidate_run_id, expected_with_aliases
            )
            match = compare_bundles(
                baseline_bundle,
                candidate_bundle,
                pair.check,
                verifier,
                expected_checks=expected_with_aliases,
                candidate_failed_checks=failed_checks,
            )
        except Exception as error:  # noqa: BLE001  fail closed: any fetch, ZIP, or model error leaves replay unverified
            return replace(base_replay, note=f"not verified: {_describe(error)}"), []

        previews, base_preview_path, cand_preview_path = (
            _extract_preview_pair(pair.sha, idx, baseline_bundle, candidate_bundle) if capture_preview else ([], "", "")
        )
        checked = replace(
            base_replay,
            match=match,
            modality=baseline_bundle.primary_modality or candidate_bundle.primary_modality or "",
            baseline_video=baseline_bundle.run_url or base_replay.baseline_video,
            candidate_video=candidate_bundle.run_url or base_replay.candidate_video,
            baseline_artifact_urls=baseline_bundle.artifact_urls,
            candidate_artifact_urls=candidate_bundle.artifact_urls,
            baseline_preview_path=base_preview_path,
            candidate_preview_path=cand_preview_path,
            candidate_failed_checks=failed_checks,
        )
        return checked, previews

    def _fetch_failed_checks(
        self,
        repo: str,
        run_id: str,
        expected_checks: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not expected_checks:
            return ()
        return _fetch_failed_job_names_from(self._github, repo, run_id)

    def _extract_run_artifact(self, repo: str, artifact: RunArtifact, *, fallback_url: str) -> list[MediaPart]:
        if not isinstance(self._github, RunArtifactsClient):
            return []
        try:
            archive_bytes = self._github.download_artifact(repo, artifact.id)
            return extract_zip_artifact(
                archive_bytes,
                source_url=artifact.archive_download_url or fallback_url,
                artifact_name=artifact.name,
            )
        except (UnsafeArchiveError, ValueError, OSError) as error:
            logger.warning("Skipping unsafe or unreadable artifact %s (%s): %s", artifact.id, artifact.name, error)
            return []

    def _mount_historical_screenshots(
        self,
        repo: str,
        required_replays: Sequence[str],
        run_id_by_sha: Mapping[str, str],
    ) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
        if not isinstance(self._github, RunArtifactsClient):
            return (), ()
        mounted_paths: list[str] = []
        extra_sources: list[dict[str, str]] = []
        total_bytes = 0

        for sha in required_replays:
            if len(mounted_paths) >= HISTORICAL_MAX_SCREENSHOTS or total_bytes >= HISTORICAL_MAX_TOTAL_BYTES:
                break
            run_id = run_id_by_sha.get(sha)
            if not run_id:
                continue
            screenshot = self._download_first_historical_screenshot(repo, run_id)
            if screenshot is None:
                continue
            if total_bytes + len(screenshot.data) > HISTORICAL_MAX_TOTAL_BYTES:
                continue
            safe_name = _safe_filename(PurePosixPath(screenshot.name).name)
            target = f"{SANDBOX_ARTIFACTS_DIR}/{sha[:12]}/{safe_name}"
            mounted_paths.append(target)
            extra_sources.append(_inline_bytes_source(target, screenshot.data))
            total_bytes += len(screenshot.data)

        return tuple(mounted_paths), tuple(extra_sources)

    def _download_first_historical_screenshot(self, repo: str, run_id: str) -> MediaPart | None:
        if not isinstance(self._github, RunArtifactsClient):
            return None
        try:
            artifacts = self._github.list_run_artifacts(repo, run_id)
        except Exception:
            logger.debug("Skipping historical artifacts for %s run %s", repo, run_id, exc_info=True)
            return None

        for artifact in _rank_active_artifacts(artifacts)[:MAX_SHARD_ARTIFACTS]:
            try:
                archive_bytes = self._github.download_artifact(repo, artifact.id)
                parts = extract_zip_artifact(archive_bytes, artifact_name=artifact.name)
            except Exception:
                logger.debug("Skipping unreadable historical artifact %s", artifact.id, exc_info=True)
                continue
            images = [
                part
                for part in parts
                if part.mime_type in _IMAGE_MIME_TYPES and len(part.data) <= HISTORICAL_MAX_SCREENSHOT_BYTES
            ]
            if images:
                return images[0]
        return None

    def _commit_proof_files(
        self,
        repo: str,
        branch: str,
        files: dict[str, Any],
        message: str,
    ) -> str:
        has_binary = any(isinstance(value, bytes) for value in files.values())
        binary_committer = getattr(self._github, "commit_binary_files", None)
        if has_binary and callable(binary_committer):
            return str(binary_committer(repo, branch, files, message))
        assert isinstance(self._github, PullRequestClient)  # noqa: S101  already verified by open_kept_pr
        return self._github.commit_files(repo, branch, files, message)

    def _compute_touched_checks(
        self,
        candidate: Candidate,
        repo: str,
        base_sha: str,
    ) -> set[Check] | None:
        if not candidate.files:
            return None if candidate.changed_files else set()
        changes: list[FileChange] = []
        for path, after in sorted(candidate.files.items()):
            before = self._github.get_file_content(repo, base_sha, path) if base_sha else None
            changes.append(FileChange(path=path, before=before, after=after))
        return touched_checks(changes)


def _load_failed_job_logs(  # noqa: PLR0913
    github: RunArtifactsClient,
    repo: str,
    run_id: str,
    *,
    check: str | None,
    expected_checks: tuple[str, ...],
    check_aliases: tuple[str, ...],
) -> bytes:
    loader: Any = github.get_failed_job_logs
    kwargs: dict[str, Any] = {"check": check}
    if _accepts_kwarg(loader, "expected_checks"):
        kwargs["expected_checks"] = expected_checks
    if _accepts_kwarg(loader, "check_aliases"):
        kwargs["check_aliases"] = check_aliases
    return bytes(loader(repo, run_id, **kwargs))


def _rank_active_artifacts(
    artifacts: Sequence[RunArtifact],
    *,
    check: str = "",
    check_aliases: Sequence[str] = (),
) -> list[RunArtifact]:
    active = [a for a in artifacts if not a.expired and 0 < a.size_in_bytes <= MAX_ZIP_DOWNLOAD_BYTES]
    labels = tuple(dict.fromkeys(label for label in (check, *check_aliases) if label and label.strip()))
    if not labels:
        return sorted(active, key=lambda artifact: (*score_artifact_name(artifact.name, ""), artifact.id))
    return sorted(
        active,
        key=lambda artifact: (min(score_artifact_name(artifact.name, label) for label in labels), artifact.id),
    )


def _extract_preview_pair(
    sha: str,
    idx: int,
    baseline: ArtifactBundle,
    candidate: ArtifactBundle,
) -> tuple[list[tuple[str, bytes]], str, str]:
    base_png = _first_capped_png(baseline)
    cand_png = _first_capped_png(candidate)
    if base_png is None or cand_png is None:
        return [], "", ""
    prefix = _safe_filename(sha[:7]) or f"bug-{idx}"
    base_path = f"{PROOF_ARTIFACTS_DIR}/{prefix}-{idx}-baseline.png"
    cand_path = f"{PROOF_ARTIFACTS_DIR}/{prefix}-{idx}-candidate.png"
    return [(base_path, base_png), (cand_path, cand_png)], base_path, cand_path


def _first_capped_png(bundle: ArtifactBundle) -> bytes | None:
    for part in bundle.parts:
        if part.mime_type == "image/png" and is_png(part.data) and len(part.data) <= PR_MAX_PREVIEW_BYTES:
            return part.data
    return None


def _filter_pr_preview_files(
    preview_files: Mapping[str, bytes] | Sequence[tuple[str, bytes]] | None,
) -> dict[str, bytes]:
    if not preview_files:
        return {}
    items = preview_files.items() if isinstance(preview_files, Mapping) else preview_files
    accepted: dict[str, bytes] = {}
    max_files = PR_MAX_PREVIEW_PAIRS * 2
    for path, data in items:
        if len(accepted) >= max_files:
            break
        if not path.startswith(f"{PROOF_ARTIFACTS_DIR}/") or ".." in PurePosixPath(path).parts:
            continue
        if is_png(data) and len(data) <= PR_MAX_PREVIEW_BYTES:
            accepted[path] = data
    return accepted


def _historical_run_id_map(
    job_runs: Sequence[JobRun],
    explicit: Mapping[str, str | int] | None,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for run in job_runs:
        if run.get("conclusion") != "failure":
            continue
        raw_run_id = run.get("run_id")
        if isinstance(raw_run_id, (int, str)) and str(raw_run_id).strip():
            mapping.setdefault(run["sha"], str(raw_run_id).strip())
    if explicit:
        for sha, run_id in explicit.items():
            if str(run_id).strip():
                mapping[sha] = str(run_id).strip()
    return mapping


def _inline_bytes_source(target: str, data: bytes) -> dict[str, str]:
    """Build the same base64 inline source payload as ``managed_agent.inline_bytes_source``."""
    return {
        "type": "inline",
        "target": target,
        "encoding": "base64",
        "content": base64.b64encode(data).decode("ascii"),
    }


def _accepts_kwarg(fn: object, name: str) -> bool:
    if not callable(fn):
        return False
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    for param in params:
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == name and param.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            return True
    return False


def _default_run_url(repo: str, run_id: str) -> str:
    text = str(run_id).strip()
    if text.startswith(("https://", "http://")):
        return text
    if text.isdigit() and "/" in repo:
        return f"https://github.com/{repo}/actions/runs/{text}"
    return ""


def _safe_filename(name: str) -> str:
    cleaned = _SAFE_FILENAME_RE.sub("-", name).strip(".-")
    return cleaned or "artifact.png"


def _describe(error: Exception) -> str:
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__


def _pr_title(candidate: Candidate) -> str:
    subject = candidate.summary.strip().splitlines()[0] if candidate.summary.strip() else candidate.branch
    return f"pit-crew: {subject}"


def _format_check(check: Check) -> str:
    return " | ".join(sorted(check))


def _format_checks(checks: frozenset[Check] | None) -> str:
    if checks is None:
        return "- ALL checks (non-workflow file or unparseable workflow changed)"
    if not checks:
        return "- (none)"
    return "\n".join(f"- {_format_check(check)}" for check in sorted(checks, key=_format_check))


def _format_shas(shas: tuple[str, ...]) -> str:
    if not shas:
        return "- (none)"
    return "\n".join(f"- {sha}" for sha in shas)


def _format_uncovered(touched: frozenset[Check] | None, uncovered: frozenset[Check]) -> str:
    if touched is None:
        return "- Inspect repository workflows to identify checks without a recorded red commit."
    if not uncovered:
        return "- (none)"
    return "\n".join(f"- {_format_check(check)}" for check in sorted(uncovered, key=_format_check))


def _format_red_commits(commits: tuple[RedCommit, ...]) -> str:
    if not commits:
        return "- (none)"
    lines = []
    for commit in commits:
        status = "fixed" if commit.fixed else "unfixed"
        checks = ", ".join(commit.checks)
        lines.append(f"- {commit.sha} [{status}] ({commit.branch}, {commit.created_at}): {checks}")
    return "\n".join(lines)
