# noqa: INP001
"""Modal 1.x serverless ASGI deployment for Pit Crew (Pit Wall UI + webhook ingress).

Deploys immediately without pre-created secrets for Demo Mode and Ledger viewing:

    uvx modal deploy deploy/modal_app.py

To enable live Gemini 4 + GitHub Actions optimization cycles on Modal, create a
Modal secret named ``pit-crew-secrets`` (with ``GEMINI_API_KEY``, ``GITHUB_TOKEN``,
``PIT_CREW_WEBHOOK_SECRET``, and optional ``PIT_CREW_API_TOKEN``) and deploy with:

    PIT_CREW_MODAL_SECRET=pit-crew-secrets uvx modal deploy deploy/modal_app.py
"""

import os
from pathlib import Path
from typing import Any

import modal

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = "/data"
LEDGER_PATH = f"{DATA_DIR}/ledger.db"
REMOTE_BRAND_DIR = Path("/root/brand")

app = modal.App("pit-crew")
volume = modal.Volume.from_name("pit-crew-data", create_if_missing=True)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ca-certificates", "ffmpeg", "git")
    .pip_install(
        "fastapi>=0.165.0",
        "google-genai>=3.6.1",
        "httpx>=0.28.1",
        "pyyaml>=6.0.4",
        "uvicorn>=0.54.0",
    )
    .env(
        {
            "PYTHONPATH": "/root/src",
            "PIT_CREW_LEDGER_PATH": LEDGER_PATH,
            "PIT_CREW_BRAND_DIR": str(REMOTE_BRAND_DIR),
        }
    )
    .add_local_dir(REPO_ROOT / "src" / "pit_crew", remote_path="/root/src/pit_crew")
    .add_local_dir(REPO_ROOT / "brand", remote_path=str(REMOTE_BRAND_DIR))
    .add_local_dir(REPO_ROOT / "agent", remote_path="/root/agent")
)

_secret_name = os.environ.get("PIT_CREW_MODAL_SECRET", "pit-crew-secrets").strip()
_secrets = [modal.Secret.from_name(_secret_name)] if _secret_name else []


@app.function(
    image=image,
    volumes={DATA_DIR: volume},
    secrets=_secrets,
    min_containers=1,
    max_containers=1,
    scaledown_window=3600,
    timeout=1800,
)
@modal.concurrent(max_inputs=100)
@modal.asgi_app()
def web() -> Any:  # noqa: ANN401
    """Build and return the Pit Crew FastAPI ASGI application."""
    from pit_crew import managed_agent  # noqa: PLC0415
    from pit_crew.dispatcher import Dispatcher, DispatcherConfig  # noqa: PLC0415
    from pit_crew.github import HttpxGitHubClient  # noqa: PLC0415
    from pit_crew.ledger import SqliteLedger  # noqa: PLC0415
    from pit_crew.proof_bridge import ProofBridge  # noqa: PLC0415
    from pit_crew.run_tracker import RunTracker  # noqa: PLC0415
    from pit_crew.runtime_cli import _bridge_lane_runner, _LiveProofGitHubAdapter  # noqa: PLC0415
    from pit_crew.server import create_app  # noqa: PLC0415

    ledger = SqliteLedger(os.environ.get("PIT_CREW_LEDGER_PATH", LEDGER_PATH))
    tracker = RunTracker()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    webhook_secret = os.environ.get("PIT_CREW_WEBHOOK_SECRET", "").strip() or None
    api_token = os.environ.get("PIT_CREW_API_TOKEN", "").strip() or None

    dispatcher = None
    if github_token:
        gh = HttpxGitHubClient(token=github_token)
        adapter = _LiveProofGitHubAdapter(gh, token=github_token)
        bridge = ProofBridge(adapter, run_lane=_bridge_lane_runner, ask=managed_agent.ask_multimodal)
        dispatcher = Dispatcher(
            ledger,
            gh,
            config=DispatcherConfig(max_parallel_runs=10),
            run_tracker=tracker,
            use_host_workspace=True,
            proof_bridge=bridge,
        )

    return create_app(
        ledger,
        dispatcher=dispatcher,
        webhook_secret=webhook_secret,
        run_tracker=tracker,
        api_token=api_token,
        brand_dir=REMOTE_BRAND_DIR,
    )
