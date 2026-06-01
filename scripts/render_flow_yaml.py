#!/usr/bin/env python3
"""
flow_intent.json → Maestro YAML renderer.

The LLM writes `flow_intent.json` (constrained by schemas/flow_intent.schema.json).
This script translates that JSON into well-formed Maestro YAML deterministically,
so the LLM never has to produce indented YAML or remember Maestro's exact step
syntax.

Maestro YAML primer (the subset we emit):

  appId: com.example.app
  ---
  - launchApp
  - tapOn: "Email"
  - inputText: "test@example.com"
  - tapOn:
      text: "Log in"
      optional: true
  - scroll
  - scroll
  - swipe:
      direction: UP
  - waitForAnimationToEnd:
      timeout: 1500
  - back

Two surface modes:
  - CLI:   `python3 scripts/render_flow_yaml.py <intent.json> [--out path.yaml]`
  - Lib:   `render_flow(intent: dict) -> str`

Validates `intent` against schemas/flow_intent.schema.json when jsonschema is
installed; degrades to a warning otherwise (renderer still runs).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "flow_intent.schema.json"


# ── Public API ───────────────────────────────────────────────────────────────

def render_flow(intent: dict, *, platform: str = "android") -> str:
    """Render a flow_intent dict as a Maestro YAML string for the given platform."""
    overrides = intent.get("platform_overrides") or {}
    if platform == "android" and overrides.get("android_app_id"):
        app_id = overrides["android_app_id"]
    elif platform == "ios" and overrides.get("ios_app_id"):
        app_id = overrides["ios_app_id"]
    else:
        app_id = intent.get("app_id", "")

    if not app_id:
        raise ValueError("flow_intent: app_id is required (or a platform-specific override)")

    lines: list[str] = []
    lines.append(f"appId: {_quote(app_id)}")
    lines.append("---")

    # Optional login pre-flight — only injected if intent.login.required.
    login = intent.get("login") or {}
    if login.get("required"):
        lines.extend(_render_login_steps(login))

    for step in (intent.get("steps") or []):
        lines.extend(_render_step(step))

    # Post-login assertions (rendered after steps so they fail the run early
    # if login silently bounced the user back; Maestro continues to the next
    # step on failure when optional=true, so we keep them required).
    for label in (intent.get("post_login_assertions") or []):
        lines.append(f"- assertVisible: {_quote(label)}")

    # Trailing newline so editors don't complain
    return "\n".join(lines) + "\n"


# ── Step renderers ───────────────────────────────────────────────────────────

def _render_login_steps(login: dict) -> list[str]:
    email_label = login.get("email_field_label") or "Email"
    password_label = login.get("password_field_label") or "Password"
    submit_label = login.get("submit_label") or "Sign in"
    email_value = login.get("email_value") or "test@example.com"
    password_value = login.get("password_value") or "Test1234!"
    out: list[str] = []
    out.append("# --- generated login pre-flight ---")
    out.append(f"- tapOn: {_quote(email_label)}")
    out.append(f"- inputText: {_quote(email_value)}")
    out.append(f"- tapOn: {_quote(password_label)}")
    out.append(f"- inputText: {_quote(password_value)}")
    out.append("- hideKeyboard")
    out.append(f"- tapOn: {_quote(submit_label)}")
    out.append("- waitForAnimationToEnd:")
    out.append("    timeout: 5000")
    out.append("# --- end generated login pre-flight ---")
    return out


def _render_step(step: dict) -> list[str]:
    """One semantic step → 1..N Maestro steps. Almost every step is rendered
    with `optional: true` so a single mis-guessed label never aborts the run.
    Set step['required']=true to opt out per step."""
    kind = step.get("kind")
    optional = not step.get("required", False)
    label_comment = step.get("screen_label")
    out: list[str] = []

    if label_comment:
        out.append(f"# {label_comment}")

    if kind == "launch":
        if optional:
            out.append("- launchApp:")
            out.append("    clearState: false")
        else:
            out.append("- launchApp")
        return out

    if kind == "wait_for_visible":
        text = step.get("label") or ""
        out.append("- assertVisible:")
        out.append(f"    text: {_quote(text)}")
        if optional:
            out.append("    optional: true")
        return out

    if kind == "tap_label":
        text = step.get("label") or ""
        out.append("- tapOn:")
        out.append(f"    text: {_quote(text)}")
        if optional:
            out.append("    optional: true")
        return out

    if kind == "tap_index":
        idx = step.get("index", 0)
        out.append("- tapOn:")
        out.append(f"    index: {idx}")
        if optional:
            out.append("    optional: true")
        return out

    if kind == "input_text":
        text = step.get("text") or ""
        out.append(f"- inputText: {_quote(text)}")
        return out

    if kind == "scroll":
        count = max(1, int(step.get("scroll_count") or 1))
        for _ in range(count):
            out.append("- scroll")
        return out

    if kind == "swipe":
        direction = step.get("direction") or "UP"
        out.append("- swipe:")
        out.append(f"    direction: {direction}")
        if step.get("duration_ms"):
            out.append(f"    duration: {int(step['duration_ms'])}")
        return out

    if kind == "back":
        out.append("- back")
        return out

    if kind == "wait_for_animation":
        out.append("- waitForAnimationToEnd:")
        out.append(f"    timeout: {int(step.get('duration_ms') or 1500)}")
        return out

    if kind == "take_screenshot":
        name = (step.get("label") or "screenshot").replace(" ", "_")
        out.append(f"- takeScreenshot: {_quote(name)}")
        return out

    # Composite high-level steps the LLM finds easier than spelling out raw maestro
    if kind == "tap_list_item":
        idx = step.get("index", 0)
        out.append("- tapOn:")
        out.append(f"    index: {idx}")
        if optional:
            out.append("    optional: true")
        out.append("- waitForAnimationToEnd:")
        out.append("    timeout: 1500")
        return out

    if kind == "type_search":
        text = step.get("text") or ""
        label = step.get("label") or "Search"
        out.append("- tapOn:")
        out.append(f"    text: {_quote(label)}")
        if optional:
            out.append("    optional: true")
        out.append(f"- inputText: {_quote(text)}")
        out.append("- hideKeyboard")
        return out

    if kind == "open_detail_then_back":
        idx = step.get("index", 0)
        out.append("- tapOn:")
        out.append(f"    index: {idx}")
        if optional:
            out.append("    optional: true")
        out.append("- waitForAnimationToEnd:")
        out.append("    timeout: 1500")
        out.append("- back")
        return out

    # Unknown kind — render as a YAML comment so the operator notices.
    out.append(f"# WARNING: unknown step kind {kind!r} skipped")
    return out


# ── Helpers ──────────────────────────────────────────────────────────────────

def _quote(s: str) -> str:
    """Always double-quote scalar values to stay safe across YAML's many edge cases
    (leading dashes, colons, '#', booleans, numbers)."""
    if s is None:
        return '""'
    # Escape backslashes and double-quotes.
    escaped = s.replace("\\", "\\\\").replace("\"", "\\\"")
    return f"\"{escaped}\""


def _validate(intent: dict) -> list[str]:
    try:
        from jsonschema import Draft7Validator
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        v = Draft7Validator(schema)
        return [f"{list(e.path)}: {e.message}" for e in v.iter_errors(intent)]
    except ImportError:
        return []
    except Exception as e:
        return [f"validation skipped: {e}"]


# ── CLI ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Render flow_intent.json as Maestro YAML.")
    ap.add_argument("intent", type=Path, help="Path to flow_intent.json")
    ap.add_argument("--out", type=Path, default=None, help="Write to this file (default: stdout)")
    ap.add_argument("--platform", choices=["android", "ios"], default="android")
    ap.add_argument("--no-validate", action="store_true", help="Skip jsonschema validation")
    args = ap.parse_args()

    if not args.intent.is_file():
        print(f"ERROR: {args.intent} not found", file=sys.stderr)
        return 2
    intent = json.loads(args.intent.read_text(encoding="utf-8"))

    if not args.no_validate:
        errs = _validate(intent)
        if errs:
            print("WARN: flow_intent schema violations:", file=sys.stderr)
            for e in errs:
                print(f"  - {e}", file=sys.stderr)

    yaml = render_flow(intent, platform=args.platform)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(yaml, encoding="utf-8")
        print(f"wrote {args.out} ({len(yaml)} bytes)", file=sys.stderr)
    else:
        sys.stdout.write(yaml)
    return 0


if __name__ == "__main__":
    sys.exit(main())
