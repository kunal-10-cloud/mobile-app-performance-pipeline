#!/usr/bin/env python3
"""
Wake an Emergent pod before the audit pipeline tries to talk to it.

The authoritative "is the pod reachable?" signal is whether the e1 MCP gateway
can run a command on it — that check lives with the orchestrating LLM (the
SKILL), not here, because only the LLM has the MCP tools. This script owns the
one thing it CAN do reliably from a plain HTTP client: POST the platform's
restart-environment endpoint to wake a sleeping pod.

  Wake:
    POST https://api.emergent.sh/jobs/v0/{job_id}/restart-environment?upgrade=false&source=manual_wakeup
    Authorization: Bearer $EMERGENT_AUTH_TOKEN
    Content-Length: 0
    → 200 {"status":"success",...} on success.
    Retry up to N times, 2s delay. Wait ~15s after success for the pod to boot.

NOTE on the status pre-check: an earlier design probed
`GET /trajectories/v0/stream` to decide awake-vs-asleep, but that endpoint
returns 403 for valid pod-scoped tokens — it's not a reliable signal. The
restart POST is idempotent enough (waking an already-awake pod is a no-op
restart) that we just call it. The SKILL gates the call on an MCP reachability
probe so we don't restart needlessly.

Auth: `$EMERGENT_AUTH_TOKEN` must be set. NEVER prompt.

Exit codes:
  0  restart POST succeeded (pod is booting / awake), or --check-only completed
  2  EMERGENT_AUTH_TOKEN not set
  3  restart failed after retries
  4  unexpected error

Modes:
  (default)            : POST restart-environment, wait, exit 0 on success.
  --check-only         : best-effort status probe via the stream endpoint;
                         print {"status": ...} and exit 0 without waking.
                         (Informational only — the stream endpoint is flaky.)
  --max-retries N      : restart attempts before giving up (default 3)
  --wait-after-wake S  : seconds to wait after a successful restart (default 15)

Usage:
  python3 scripts/wake_pod.py <job_id>
  python3 scripts/wake_pod.py <job_id> --wait-after-wake 25
  python3 scripts/wake_pod.py <job_id> --check-only
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

PLATFORM_HOST = "https://api.emergent.sh"
# Sentinel last_request_id used by the platform API as a "no prior request"
# stream cursor. Same value the web pipeline uses; arbitrary UUID.
SEED_LAST_REQUEST_ID = "5b6feb4e-b686-4e22-82f5-87aeee44fb32"
ORIGIN = "https://app.emergent.sh"


def _headers(token: str, extra: dict | None = None) -> dict:
    h = {
        "accept": "text/event-stream",
        "authorization": f"Bearer {token}",
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": ORIGIN,
    }
    if extra:
        h.update(extra)
    return h


def _http(method: str, url: str, headers: dict, *, body: bytes | None = None, timeout: float = 30.0) -> tuple[int, bytes]:
    """HTTP via curl when available, falling back to urllib.

    The platform API sits behind a WAF (Cloudflare) that fingerprints the
    client. Python's urllib default TLS/UA gets served a 403 challenge page,
    while curl passes cleanly — confirmed empirically. So we prefer curl and
    only fall back to urllib (with a browser UA) if curl is missing. Returns
    (status, body_bytes)."""
    if shutil.which("curl"):
        return _http_curl(method, url, headers, body=body, timeout=timeout)
    return _http_urllib(method, url, headers, body=body, timeout=timeout)


def _http_curl(method: str, url: str, headers: dict, *, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    cmd = ["curl", "-s", "-X", method, "--max-time", str(int(timeout)),
           "-w", "\n__HTTP_STATUS__:%{http_code}"]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if body is not None:
        # POST with empty/no body still needs the request to carry through.
        cmd += ["--data-binary", body.decode("utf-8", "replace")]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 0, f"curl error: {e}".encode()
    out = r.stdout or b""
    marker = b"\n__HTTP_STATUS__:"
    if marker in out:
        payload, _, status_str = out.rpartition(marker)
        try:
            return int(status_str.strip() or b"0"), payload
        except ValueError:
            return 0, payload
    return (0 if r.returncode != 0 else 200), out


def _http_urllib(method: str, url: str, headers: dict, *, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    # Browser-ish UA to reduce (not eliminate) WAF challenges when curl is absent.
    h = dict(headers)
    h.setdefault("user-agent",
                 "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.getcode(), r.read()
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 0, str(e).encode("utf-8", "replace")


def check_pod_awake(job_id: str, token: str, *, timeout: float = 15.0) -> tuple[str, str]:
    """Return ("awake" | "asleep" | "unknown", short_diagnostic)."""
    url = f"{PLATFORM_HOST}/trajectories/v0/stream?job_id={job_id}&last_request_id={SEED_LAST_REQUEST_ID}"
    status, body = _http("GET", url, _headers(token), timeout=timeout)
    if status == 0:
        return "unknown", f"network error: {body.decode('utf-8','replace')[:200]}"
    if status == 401 or status == 403:
        return "unknown", f"auth rejected ({status})"
    if status >= 500:
        return "unknown", f"platform error {status}"
    if status == 200:
        # SSE: pod awake = stream emits at least one event with data; sleeping
        # = empty body or only heartbeats. Heuristic: any line starting with
        # `data:` and a non-empty payload counts as awake.
        text = body.decode("utf-8", "replace")
        for line in text.splitlines():
            if line.startswith("data:") and line[5:].strip():
                return "awake", "stream emitted data"
        return "asleep", "stream returned no data events"
    return "unknown", f"unexpected status {status}"


def wake_pod(job_id: str, token: str, *, max_retries: int = 3, wait_after_wake: int = 15) -> tuple[bool, str]:
    """POST restart-environment. Returns (ok, diagnostic)."""
    url = f"{PLATFORM_HOST}/jobs/v0/{job_id}/restart-environment?upgrade=false&source=manual_wakeup"
    headers = _headers(token, {"content-length": "0"})
    last_err = ""
    for attempt in range(1, max_retries + 1):
        status, body = _http("POST", url, headers, body=b"", timeout=30.0)
        if status in (200, 201, 202, 204):
            time.sleep(wait_after_wake)
            return True, f"wake POST attempt {attempt} returned {status}; waited {wait_after_wake}s"
        last_err = f"attempt {attempt}: status {status} body {body.decode('utf-8','replace')[:200]}"
        if attempt < max_retries:
            time.sleep(2)
    return False, last_err


def main() -> int:
    ap = argparse.ArgumentParser(description="Check / wake an Emergent pod.")
    ap.add_argument("job_id")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--max-retries", type=int, default=3)
    ap.add_argument("--wait-after-wake", type=int, default=15)
    args = ap.parse_args()

    token = os.environ.get("EMERGENT_AUTH_TOKEN") or ""
    if not token:
        print("ERROR: EMERGENT_AUTH_TOKEN not set in environment. The pipeline never prompts for a token; the platform is expected to inject it.", file=sys.stderr)
        return 2

    if args.check_only:
        # Best-effort informational probe only. The stream endpoint is flaky
        # (403 with valid tokens), so we never gate on it.
        status, diag = check_pod_awake(args.job_id, token)
        print(json.dumps({"status": status, "diagnostic": diag}))
        return 0

    # Default: POST restart-environment. Waking an already-awake pod is a
    # tolerable no-op restart; the SKILL only calls this when an MCP probe
    # showed the pod unreachable, so over-restarting is not a concern.
    ok, wake_diag = wake_pod(args.job_id, token, max_retries=args.max_retries, wait_after_wake=args.wait_after_wake)
    print(json.dumps({"action": "restart-environment", "ok": ok, "diagnostic": wake_diag}))
    if not ok:
        print(f"ERROR: wake failed after {args.max_retries} retries — {wake_diag}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
