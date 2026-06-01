#!/usr/bin/env python3
"""
Stage 4d.3 — Generate a baseline `flow_intent.json` from `screen_map.json`.

Pure templating — NO LLM. The output is a structured flow_intent (see
schemas/flow_intent.schema.json) that exercises:
  - launchApp
  - one tap per detected tab (scroll x 3 inside each)
  - waitForAnimationToEnd between tab switches

Every step is rendered with `required=false` so a single mis-guessed tab label
doesn't abort the whole flow. The refine_flow_with_llm.py step layers login,
realistic interactions, and back-navigation on top of this draft.

Two outputs:
  flows/draft_intent.json   — structured (LLM and renderer both consume this)
  flows/draft.yaml          — rendered immediately for diagnostic / fallback use
                              (if the refine step is skipped, this is what Maestro runs)

Usage:
  python3 scripts/generate_draft_flow.py <audit_id>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from render_flow_yaml import render_flow  # noqa: E402


SCROLLS_PER_TAB = 3


def build_draft_intent(screen_map: dict) -> dict:
    nav = screen_map.get("navigation") or {}
    android_id = screen_map.get("bundle_id_android") or "unknown.app"
    ios_id = screen_map.get("bundle_id_ios") or "unknown.app"

    steps: list[dict] = []
    steps.append({"kind": "launch", "required": True, "screen_label": "Launch"})
    steps.append({"kind": "wait_for_animation", "duration_ms": 3000, "screen_label": "Warm-up wait"})

    tabs = nav.get("tabs") or []
    if tabs:
        for t in tabs:
            label = t.get("label_guess") or t.get("route") or ""
            if not label:
                continue
            steps.append({
                "kind": "tap_label",
                "label": label,
                "required": False,
                "screen_label": f"Tab: {label}",
            })
            steps.append({
                "kind": "wait_for_animation",
                "duration_ms": 800,
            })
            steps.append({
                "kind": "scroll",
                "scroll_count": SCROLLS_PER_TAB,
                "screen_label": f"Scroll {label}",
            })
    else:
        # No tabs — assume single-screen app; scroll on the entry screen.
        steps.append({
            "kind": "scroll",
            "scroll_count": SCROLLS_PER_TAB,
            "screen_label": "Scroll entry screen",
        })

    intent = {
        "app_id": android_id or ios_id,
        "platform_overrides": {
            "android_app_id": android_id,
            "ios_app_id": ios_id,
        },
        "login": {
            # Login is OFF in the draft; refine_flow_with_llm flips this on
            # when screen_map.auth.detected is true.
            "required": False,
        },
        "steps": steps,
        "post_login_assertions": [],
        "metadata": {
            "source": "draft",
            "model_name": None,
            "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }
    return intent


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate a baseline Maestro flow from screen_map.json.")
    ap.add_argument("audit_id")
    args = ap.parse_args()

    base = Path(os.environ.get("MOBILE_AUDIT_RUNS_DIR", ".audit-runs"))
    audit_dir = base / args.audit_id
    flows_dir = audit_dir / "flows"
    screen_map_path = flows_dir / "screen_map.json"
    if not screen_map_path.is_file():
        print(f"ERROR: screen_map.json not found at {screen_map_path}", file=sys.stderr)
        return 2

    screen_map = json.loads(screen_map_path.read_text(encoding="utf-8"))
    intent = build_draft_intent(screen_map)

    (flows_dir / "draft_intent.json").write_text(json.dumps(intent, indent=2), encoding="utf-8")
    # Render Android by default — Maestro picks up the iOS appId via override at run time.
    yaml = render_flow(intent, platform="android")
    (flows_dir / "draft.yaml").write_text(yaml, encoding="utf-8")

    print(f"Draft flow generated → {flows_dir / 'draft_intent.json'} (+ draft.yaml)", file=sys.stderr)
    print(f"  steps: {len(intent['steps'])}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
