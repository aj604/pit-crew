import argparse
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pit_crew import runtime_cli
from pit_crew import workspace as host_workspace
from pit_crew.contracts import Evidence, Verdict
from pit_crew.dispatcher import Dispatcher
from pit_crew.github import HttpxGitHubClient, PullRequestClient
from pit_crew.ledger import SqliteLedger
from pit_crew.proof_bridge import ProofBridge, ProofHistoryClient


class _FakeDispatcher:
    def __init__(self, verdict: Verdict | None = None, verdicts: list[Verdict] | None = None) -> None:
        self._verdict = verdict
        self._verdicts = verdicts if verdicts is not None else ([verdict] if verdict is not None else [])
        self.calls: list[tuple[str, str, Path | None]] = []

    def run_cycle(self, repo: str) -> Verdict | None:
        self.calls.append(("run_cycle", repo, host_workspace.configured_workspace()))
        return self._verdict

    def run_until_sla(self, repo: str) -> list[Verdict]:
        self.calls.append(("run_until_sla", repo, host_workspace.configured_workspace()))
        return list(self._verdicts)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    runtime_cli.add_runtime_subcommands(sub)
    return parser


def test_run_cycle_subcommand_parses_defaults_and_reports_verdict_summary(tmp_path: Path) -> None:
    ledger_path = tmp_path / "ledger.db"
    ledger = SqliteLedger(ledger_path)
    ledger.add(
        Evidence(
            id="ev-1",
            candidate_id="cand-1",
            baseline_seconds=(100.0,) * 5,
            candidate_seconds=(48.0,) * 5,
            bugs_total=3,
            bugs_caught=3,
        )
    )
    verdict = Verdict(
        id="v-1",
        candidate_id="cand-1",
        keep=True,
        speedup=2.08,
        p_value=0.004,
        reason="2.08x faster, caught 3/3 bugs (SLA met)",
        median_seconds=48.0,
        p95_seconds=50.0,
        sla_met=True,
    )
    ws = tmp_path / "checkout"
    ws.mkdir()

    args = _parser().parse_args(
        ["run-cycle", "--repo", "org/fork", "--ledger", str(ledger_path), "--workspace", str(ws)]
    )
    assert args.timing_pairs == 5
    assert args.parallel_runs == 10
    assert args.until_sla is False
    assert args.max_cycles == 5
    assert args.workflow == "ci.yml"
    assert args.no_open_pr is False
    assert args.base_branch == "main"

    fake_disp = _FakeDispatcher(verdict=verdict)
    summary, passed = runtime_cli.run_runtime_command(args, dispatcher=fake_disp)

    assert passed is True
    assert summary["command"] == "run-cycle"
    assert summary["repo"] == "org/fork"
    assert summary["keep"] is True
    assert summary["speedup"] == 2.08
    assert summary["p_value"] == 0.004
    assert summary["median_seconds"] == 48.0
    assert summary["p95_seconds"] == 50.0
    assert summary["sla_met"] is True
    assert summary["bugs_caught"] == 3
    assert summary["bugs_total"] == 3
    assert isinstance(summary["elapsed_seconds"], float)
    assert fake_disp.calls == [("run_cycle", "org/fork", ws)]


def test_run_cycle_until_sla_empty_verdict_and_missing_workspace_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path = tmp_path / "ledger.db"
    args_sla = _parser().parse_args(["run-cycle", "--repo", "org/fork", "--ledger", str(ledger_path), "--until-sla"])
    fake_disp = _FakeDispatcher(verdicts=[])
    summary, passed = runtime_cli.run_runtime_command(args_sla, dispatcher=fake_disp)

    assert passed is False
    assert summary["reason"] == "no verdict produced"
    assert fake_disp.calls == [("run_until_sla", "org/fork", None)]

    # Missing workspace directory fails cleanly and redacts secrets if present in error text.
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_supersecret123")
    args_bad_ws = _parser().parse_args(
        [
            "run-cycle",
            "--repo",
            "org/fork",
            "--ledger",
            str(ledger_path),
            "--workspace",
            str(tmp_path / "missing-ghp_supersecret123"),
        ]
    )
    err_summary, err_passed = runtime_cli.run_runtime_command(args_bad_ws, dispatcher=fake_disp)
    assert err_passed is False
    assert "FileNotFoundError" in err_summary["error"]
    assert "ghp_supersecret123" not in json.dumps(err_summary)
    assert "[REDACTED]" in err_summary["error"]


def test_run_cycle_builds_live_dispatcher_when_not_injected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test_token")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/org/fork":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path == "/repos/org/fork/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "abc1234"}})
        return httpx.Response(404, json={"message": "not found"})

    gh = HttpxGitHubClient(token="ghs_test_token", transport=httpx.MockTransport(handler))  # noqa: S106
    ws = tmp_path / "ws"
    ws.mkdir()

    # Patch Dispatcher.run_cycle to verify live wiring parameters without calling Gemini.
    seen_configs: list[tuple[int, bool, object]] = []

    def fake_run_cycle(self: Dispatcher, _repo: str) -> Verdict | None:
        seen_configs.append((self.config.max_parallel_runs, self._use_host_workspace, self._proof_bridge))
        return None

    monkeypatch.setattr(Dispatcher, "run_cycle", fake_run_cycle)
    args = _parser().parse_args(
        [
            "run-cycle",
            "--repo",
            "org/fork",
            "--ledger",
            str(tmp_path / "live.db"),
            "--parallel-runs",
            "12",
            "--no-open-pr",
            "--workspace",
            str(ws),
        ]
    )
    summary, passed = runtime_cli.run_runtime_command(args, github=gh)
    gh.close()

    assert passed is False
    assert summary["command"] == "run-cycle"
    assert len(seen_configs) == 1
    assert seen_configs[0][0] == 12
    assert seen_configs[0][1] is True
    assert seen_configs[0][2] is not None


def test_serve_runs_without_github_token_and_wires_live_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger_path = tmp_path / "serve.db"
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PIT_CREW_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("PIT_CREW_API_TOKEN", raising=False)

    captured: list[tuple[FastAPI, str, int]] = []

    def fake_runner(app: FastAPI, *, host: str, port: int) -> None:
        captured.append((app, host, port))

    args = _parser().parse_args(["serve", "--ledger", str(ledger_path), "--host", "0.0.0.0", "--port", "9001"])  # noqa: S104
    summary, passed = runtime_cli.run_runtime_command(args, serve_runner=fake_runner)

    assert passed is True
    assert summary["dispatcher_enabled"] is False
    app_demo, host, port = captured[0]
    assert (host, port) == ("0.0.0.0", 9001)  # noqa: S104
    assert app_demo.state.dispatcher is None
    with TestClient(app_demo) as client:
        assert client.get("/health").json() == {"status": "ok", "ledger": "sqlite"}
        assert client.post("/triggers/cycle", json={"repo": "org/fork"}).status_code == 503

    # Now configure GITHUB_TOKEN, PIT_CREW_WEBHOOK_SECRET, and PIT_CREW_API_TOKEN.
    captured.clear()
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_live_tok")
    monkeypatch.setenv("PIT_CREW_WEBHOOK_SECRET", "whsec_123")
    monkeypatch.setenv("PIT_CREW_API_TOKEN", "apitok_456")

    args_live = _parser().parse_args(["serve", "--ledger", str(ledger_path), "--parallel-runs", "8"])
    summary_live, passed_live = runtime_cli.run_runtime_command(args_live, serve_runner=fake_runner)

    assert passed_live is True
    assert summary_live["dispatcher_enabled"] is True
    app_live, _, _ = captured[0]
    assert app_live.state.dispatcher is not None
    assert app_live.state.dispatcher.config.max_parallel_runs == 8
    assert app_live.state.webhook_secret == "whsec_123"  # noqa: S105
    assert app_live.state.api_token == "apitok_456"  # noqa: S105
    with TestClient(app_live) as client:
        assert client.get("/api/repos/org/fork/status").status_code == 401
        assert (
            client.get("/api/repos/org/fork/status", headers={"Authorization": "Bearer apitok_456"}).status_code == 200
        )


def test_register_webhook_validates_https_upserts_idempotently_and_redacts_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_val = "whsec_ultra_secret_value"  # noqa: S105
    token_val = "ghp_ultra_secret_token"  # noqa: S105
    monkeypatch.setenv("PIT_CREW_WEBHOOK_SECRET", secret_val)
    monkeypatch.setenv("GITHUB_TOKEN", token_val)

    # 1. Plain HTTP rejected without --allow-insecure-http
    bad_args = _parser().parse_args(
        ["register-webhook", "--repo", "org/fork", "--url", "http://example.com/webhooks/github"]
    )
    bad_summary, bad_passed = runtime_cli.run_runtime_command(bad_args)
    assert bad_passed is False
    assert "https://" in bad_summary["error"]

    # 2. First call creates hook + pings; second call PATCHes existing hook + pings
    hooks_store: list[dict[str, Any]] = []
    requests_log: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content.decode()) if request.content else {}
        requests_log.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/repos/org/fork/hooks":
            return httpx.Response(200, json=hooks_store)
        if request.method == "POST" and request.url.path == "/repos/org/fork/hooks":
            created: dict[str, Any] = {"id": 42, **body}
            hooks_store.append(created)
            return httpx.Response(201, json=created)
        if request.method == "PATCH" and request.url.path == "/repos/org/fork/hooks/42":
            hooks_store[0].update(body)
            return httpx.Response(200, json=hooks_store[0])
        if request.method == "POST" and request.url.path == "/repos/org/fork/hooks/42/pings":
            return httpx.Response(204)
        return httpx.Response(500, text=f"boom {secret_val} {token_val}")

    with httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler)) as mock_http:
        ok_args = _parser().parse_args(
            ["register-webhook", "--repo", "org/fork", "--url", "https://pitcrew.example.com/webhooks/github"]
        )
        s1, p1 = runtime_cli.run_runtime_command(ok_args, github=mock_http)
        assert (p1, s1["action"], s1["hook_id"], s1["pinged"]) == (True, "created", 42, True)
        assert secret_val not in json.dumps(s1)
        assert token_val not in json.dumps(s1)

        s2, p2 = runtime_cli.run_runtime_command(ok_args, github=mock_http)
        assert (p2, s2["action"], s2["hook_id"], s2["pinged"]) == (True, "updated", 42, True)
        assert secret_val not in json.dumps(s2)

        # Allow http:// when --allow-insecure-http is passed; trigger 500 on unknown repo to test secret redaction.
        http_args = _parser().parse_args(
            [
                "register-webhook",
                "--repo",
                "other/repo",
                "--url",
                "http://127.0.0.1:8000/webhooks/github",
                "--allow-insecure-http",
            ]
        )
        s3, p3 = runtime_cli.run_runtime_command(http_args, github=mock_http)
        assert p3 is False
        assert secret_val not in json.dumps(s3)
        assert token_val not in json.dumps(s3)


def test_register_webhook_missing_secrets_and_standalone_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PIT_CREW_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    code = runtime_cli.main(
        ["register-webhook", "--repo", "org/fork", "--url", "https://pitcrew.example.com/webhooks/github"]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert "PIT_CREW_WEBHOOK_SECRET" in out["error"]

    monkeypatch.setenv("PIT_CREW_WEBHOOK_SECRET", "sec")
    code2 = runtime_cli.main(
        ["register-webhook", "--repo", "org/fork", "--url", "https://pitcrew.example.com/webhooks/github"]
    )
    out2 = json.loads(capsys.readouterr().out)
    assert code2 == 1
    assert "GITHUB_TOKEN" in out2["error"]

    with pytest.raises(ValueError, match="Unsupported runtime command"):
        runtime_cli.run_runtime_command(argparse.Namespace(command="unknown"))


def _mp4_zip_bytes(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w") as zf:
        zf.writestr("logs/notes.txt", b"text")
        zf.writestr("recordings/replay.mp4", payload)
    return buf.getvalue()


def _adapter_routes(zip_bytes: bytes) -> dict[tuple[str, str], httpx.Response]:
    return {
        ("GET", "/repos/org/fork/contents/.github/workflows/ci.yml"): httpx.Response(
            200, json={"content": "bmFtZTogQ0kK", "encoding": "base64"}
        ),
        ("GET", "/repos/org/fork/actions/runs"): httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "id": 10,
                        "head_sha": "deadbeef",
                        "head_branch": "main",
                        "created_at": "2026-11-01T00:00:00Z",
                        "name": "CI",
                        "run_attempt": 1,
                    }
                ]
            },
        ),
        ("GET", "/repos/org/fork/actions/runs/10/jobs"): httpx.Response(
            200, json={"jobs": [{"id": 100, "name": "test", "conclusion": "failure"}]}
        ),
        ("GET", "/repos/org/fork/actions/runs/10/artifacts"): httpx.Response(
            200, json={"artifacts": [{"id": 55, "name": "replay", "size_in_bytes": 120, "expired": False}]}
        ),
        ("GET", "/repos/org/fork/actions/artifacts/55/zip"): httpx.Response(200, content=zip_bytes),
        ("GET", "/repos/org/fork/actions/runs/20/artifacts"): httpx.Response(
            200, json={"artifacts": [{"id": 56, "name": "replay", "size_in_bytes": 64, "expired": False}]}
        ),
        ("GET", "/repos/org/fork/actions/artifacts/56/zip"): httpx.Response(200, content=b"not-a-zip"),
        ("GET", "/repos/org/fork/actions/runs/20/jobs"): httpx.Response(
            200, json={"jobs": [{"id": 200, "name": "test", "conclusion": "failure"}]}
        ),
        ("GET", "/repos/org/fork/actions/jobs/200/logs"): httpx.Response(200, content=b"AssertionError: boom"),
        ("GET", "/repos/org/fork/git/ref/heads/pc/cand-1"): httpx.Response(200, json={"object": {"sha": "tip-sha-1"}}),
        ("GET", "/repos/org/fork/git/commits/tip-sha-1"): httpx.Response(200, json={"tree": {"sha": "tree-1"}}),
        ("POST", "/repos/org/fork/git/trees"): httpx.Response(201, json={"sha": "tree-2"}),
        ("POST", "/repos/org/fork/git/commits"): httpx.Response(201, json={"sha": "card-sha-2"}),
        ("PATCH", "/repos/org/fork/git/refs/heads/pc/cand-1"): httpx.Response(
            200, json={"object": {"sha": "card-sha-2"}}
        ),
        ("POST", "/repos/org/fork/pulls"): httpx.Response(201, json={"html_url": "https://github.com/org/fork/pull/5"}),
    }


def test_live_proof_github_adapter_satisfies_protocols_and_fetches_artifacts_and_logs() -> None:
    routes = _adapter_routes(_mp4_zip_bytes(b"fake-mp4-video"))

    def handler(request: httpx.Request) -> httpx.Response:
        return routes.get((request.method, request.url.path), httpx.Response(404))

    transport = httpx.MockTransport(handler)
    with (
        HttpxGitHubClient(token="tok", transport=transport) as gh,  # noqa: S106
        httpx.Client(base_url="https://api.github.com", transport=transport) as http_client,
    ):
        adapter: ProofHistoryClient = runtime_cli._LiveProofGitHubAdapter(gh, http_client=http_client)  # noqa: SLF001
        assert isinstance(adapter, PullRequestClient)
        assert ProofBridge(adapter) is not None

        assert adapter.get_file_content("org/fork", "base123", ".github/workflows/ci.yml") == "name: CI\n"
        jobs = adapter.list_workflow_job_runs("org/fork")
        assert len(jobs) == 1
        assert jobs[0]["sha"] == "deadbeef"
        assert adapter.get_run_failure_output("org/fork", "10") == b"fake-mp4-video"
        assert adapter.get_run_failure_output("org/fork", "20") == b"AssertionError: boom"
        pr_adapter = runtime_cli._LiveProofGitHubAdapter(gh, http_client=http_client)  # noqa: SLF001
        assert pr_adapter.commit_files("org/fork", "pc/cand-1", {"a.svg": "<svg/>"}, "msg") == "card-sha-2"
        assert (
            pr_adapter.open_pull_request("org/fork", head="pc/cand-1", base="main", title="t", body="b")
            == "https://github.com/org/fork/pull/5"
        )
        assert len(pr_adapter.list_run_artifacts("org/fork", 10)) == 1
        assert pr_adapter.list_failed_job_names("org/fork", 20) == ["test"]
        assert pr_adapter.get_failed_job_logs("org/fork", 20) == b"=== test ===\nAssertionError: boom"
        assert pr_adapter.download_artifact("org/fork", 56) == b"not-a-zip"
        pr_adapter.close()

    with pytest.raises(ValueError, match="Invalid repository"):
        runtime_cli._repo_api_path("invalid-slug")  # noqa: SLF001
