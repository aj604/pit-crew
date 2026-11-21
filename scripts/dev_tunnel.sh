#!/usr/bin/env bash
# Start a local HTTPS tunnel to the Pit Crew server so GitHub Actions `workflow_run`
# webhooks can reach `POST /webhooks/github` on a developer machine.
#
# Usage:
#   scripts/dev_tunnel.sh [--port 8000] [--provider auto|tailscale|cloudflared|ngrok] [--repo owner/name]

set -euo pipefail

PORT="${PORT:-8000}"
PROVIDER="auto"
REPO=""

usage() {
  cat <<'EOF'
Usage: scripts/dev_tunnel.sh [options]

Options:
  --port <port>          Local Pit Crew server port (default: $PORT or 8000)
  --provider <name>      Tunnel tool: auto | tailscale | cloudflared | ngrok (default: auto)
  --repo <owner/name>    Automatically register the tunnel URL via register-webhook (tailscale)
  -h, --help             Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --port)
      PORT="$2"
      shift 2
      ;;
    --provider)
      PROVIDER="$2"
      shift 2
      ;;
    --repo)
      REPO="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

pick_provider() {
  if [[ "$PROVIDER" != "auto" ]]; then
    echo "$PROVIDER"
    return
  fi
  if command -v tailscale >/dev/null 2>&1; then
    echo "tailscale"
  elif command -v cloudflared >/dev/null 2>&1; then
    echo "cloudflared"
  elif command -v ngrok >/dev/null 2>&1; then
    echo "ngrok"
  else
    echo "none"
  fi
}

SELECTED="$(pick_provider)"

case "$SELECTED" in
  tailscale)
    DNS_NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
    if [[ -n "$DNS_NAME" ]]; then
      WEBHOOK_URL="https://${DNS_NAME}/webhooks/github"
      echo "Webhook endpoint: ${WEBHOOK_URL}"
      if [[ -n "$REPO" ]]; then
        uv run --project "$PROJECT_ROOT" python -m pit_crew.runtime_cli register-webhook \
          --repo "$REPO" \
          --url "$WEBHOOK_URL"
      fi
    fi
    exec tailscale funnel "$PORT"
    ;;
  cloudflared)
    echo "Starting Cloudflare Quick Tunnel to http://127.0.0.1:${PORT} ..."
    echo "Append /webhooks/github to the https://*.trycloudflare.com URL printed below."
    exec cloudflared tunnel --url "http://127.0.0.1:${PORT}"
    ;;
  ngrok)
    echo "Starting ngrok HTTPS tunnel to http://127.0.0.1:${PORT} ..."
    echo "Append /webhooks/github to the Forwarding https:// URL printed by ngrok."
    exec ngrok http "$PORT"
    ;;
  *)
    echo "Error: None of 'tailscale', 'cloudflared', or 'ngrok' was found on PATH." >&2
    exit 1
    ;;
esac
