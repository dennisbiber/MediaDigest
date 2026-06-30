#!/usr/bin/env python3
"""Install the digest's Open WebUI-side artifacts via the OWUI API.

OWUI loads Functions, Tools, and Models from its database (not from disk), so these are
a one-time import. This installs:
  - the two feedback Actions (interfaces/owui/functions)
  - the Digest Manager tool (interfaces/owui/tools/digest_manager_tool.py)
  - optionally the Digest Setup Assistant model (needs a base chat model you have)

PREREQUISITE (changed in 2.0): the OWUI wrappers are now standalone HTTP clients of
the core service — they import NO digestcore, so nothing needs to be pip-installed in
the Open WebUI container. They do need two things from the deployment, set as env vars
on the OWUI/pipelines containers (or per-function valves):
    DIGEST_CORE_URL   e.g. http://digest-core:8787   (core on the docker network)
    DIGEST_TOKEN_PATH e.g. /run/digest/owui.token     (a volume shared by both containers)
The Pipeline's on_startup generates that token, registers it with the core, and writes
it to DIGEST_TOKEN_PATH; the Tool and Actions read it. Approve it once on the core:
    digest-core auth approve owui

Reliable fallback for any item is the UI: Admin > Functions, Workspace > Tools,
Workspace > Models. The API field shapes (especially for models) vary across OWUI
versions; if a call returns non-2xx the script prints the status so you can fall back.

Usage:
    OWUI_URL=http://localhost:3000 OWUI_API_KEY=sk-... \
      [DIGEST_ASSISTANT_BASE_MODEL=llama3.1:8b] \
      python3 scripts/install_owui.py

Get an API key from Open WebUI: Settings > Account > API Keys.
If you're replacing existing copies, delete the old Function/Tool/Model in the UI first
to avoid id conflicts (this is exactly what an upgrade-in-place needs).
"""

import os
import re
import sys
import json
import pathlib
import urllib.request
import urllib.error

OWUI_URL = os.environ.get("OWUI_URL", "http://localhost:3000").rstrip("/")
API_KEY = os.environ.get("OWUI_API_KEY")
BASE_MODEL = os.environ.get("DIGEST_ASSISTANT_BASE_MODEL")  # optional
ROOT = pathlib.Path(__file__).resolve().parent.parent
OWUI_DIR = ROOT / "interfaces" / "owui"

ACTIONS = OWUI_DIR / "functions"
TOOL = OWUI_DIR / "tools" / "digest_manager_tool.py"
ASSISTANT_PROMPT = OWUI_DIR / "digest_setup_assistant.md"


def post(path: str, payload: dict):
    req = urllib.request.Request(
        f"{OWUI_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def get(path: str):
    req = urllib.request.Request(
        f"{OWUI_URL}{path}",
        headers={"Authorization": f"Bearer {API_KEY}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def _available_model_ids() -> set:
    """The exact set of model ids OWUI serves — these are the keys the chat path
    checks a custom model's base_model_id against. Empty set on any failure."""
    status, body = get("/api/models")
    if not (status and 200 <= status < 300):
        return set()
    try:
        data = json.loads(body)
        items = data.get("data", data) if isinstance(data, dict) else data
        return {m.get("id") for m in items if isinstance(m, dict) and m.get("id")}
    except Exception:  # noqa: BLE001
        return set()


def _title(content: str, fallback: str) -> str:
    m = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
    return m.group(1).strip() if m else fallback


def install_actions():
    print("Actions:")
    for fid, name in (("digest_more_like_this", "Digest - More like this"),
                      ("digest_less_like_this", "Digest - Less like this")):
        content = (ACTIONS / f"{fid}.py").read_text()
        title = _title(content, name)
        status, body = post("/api/v1/functions/create",
                            {"id": fid, "name": title, "type": "action",
                             "content": content, "meta": {"description": title}})
        if _report(fid, status, body):
            _enable_action(fid)


def install_tool():
    print("Tool:")
    tid = "digest_manager_tool"
    content = TOOL.read_text()
    status, body = post("/api/v1/tools/create",
                        {"id": tid, "name": "Digest Manager", "content": content,
                         "meta": {"description": "Helps manage Digest pipeline subscriptions and user accounts"}})
    _report(tid, status, body)


def _resolve_base_model():
    """Return the exact OWUI model id to use as the base, or None to abort.

    The chat path checks a custom model's base_model_id by *exact* membership in the
    model registry (no tag fallback), so 'llama3.1' won't match a registry key of
    'llama3.1:8b' and every chat would raise 'Model not found' until a UI save rebinds
    it. We therefore require an exact id: pass it through if it matches, auto-correct a
    bare name with exactly one tagged match, and otherwise print the real ids and stop
    rather than create a card that chat-fails."""
    ids = _available_model_ids()
    if not ids:
        print(f"  note: couldn't reach /api/models to verify '{BASE_MODEL}'; proceeding. "
              "If chat says 'Model not found', the base id isn't an exact OWUI model id.")
        return BASE_MODEL
    if BASE_MODEL in ids:
        return BASE_MODEL
    cands = sorted(i for i in ids if i.split(":")[0] == BASE_MODEL.split(":")[0])
    if len(cands) == 1:
        print(f"  note: base '{BASE_MODEL}' -> '{cands[0]}' (exact registry id).")
        return cands[0]
    print(f"  note: '{BASE_MODEL}' isn't in OWUI's model list yet (the LLM may come up "
          "after this step). Creating the card anyway; it resolves once the model is "
          "registered. If chat later says 'Model not found', re-bind it in Workspace > Models.")
    return BASE_MODEL


def install_model():
    print("Model (Digest Setup Assistant):")
    if not BASE_MODEL:
        print("  skipped — set DIGEST_ASSISTANT_BASE_MODEL to a tool-capable chat model "
              "you have (e.g. llama3.1:8b), or build it in Workspace > Models.")
        return
    base = _resolve_base_model()
    if base is None:
        return
    system = ASSISTANT_PROMPT.read_text()
    status, body = post("/api/v1/models/create",
                        {"id": "digest-setup-assistant", "name": "Digest Setup Assistant",
                         "base_model_id": base, "is_active": True,
                         # function_calling=native uses the model's real tool API instead of
                         # the prompt-template fallback, which weak models ignore (and then
                         # hallucinate). Requires a tool-capable base model.
                         "params": {"system": system, "function_calling": "native"},
                         # Bind the two rating actions to this model: digests are delivered
                         # with model=digest-setup-assistant, and the 👍/👎 buttons render
                         # from this model's actions. This makes them appear regardless of
                         # the global toggle.
                         "meta": {"description": "Conversational setup for digest subscriptions",
                                  "toolIds": ["digest_manager_tool"],
                                  "actionIds": ["digest_more_like_this", "digest_less_like_this"]}})
    _report(f"digest-setup-assistant (base={base})", status, body)


def _report(name: str, status, body) -> bool:
    if status and 200 <= status < 300:
        print(f"  {name}: OK ({status})")
        return True
    snippet = (body or "").strip().replace("\n", " ")[:160]
    print(f"  {name}: FAILED ({status}) {snippet} — import it manually via the UI.")
    return False


def _flag(body, key):
    try:
        return json.loads(body).get(key)
    except Exception:  # noqa: BLE001
        return "?"


def _enable_action(fid: str):
    """API-created functions are born is_active=False / is_global=False (the create
    form can't set them). Action buttons only show when active, and show on *every*
    message when global, so flip both on. Right after a fresh create the state is
    known-False, so a single toggle each lands them True."""
    a_status, a_body = post(f"/api/v1/functions/id/{fid}/toggle", {})
    g_status, g_body = post(f"/api/v1/functions/id/{fid}/toggle/global", {})
    if a_status and 200 <= a_status < 300:
        print(f"    enabled: is_active={_flag(a_body, 'is_active')}, "
              f"is_global={_flag(g_body, 'is_global')}")
    else:
        print(f"    enable FAILED ({a_status}) — toggle it on in Admin > Functions.")


def _preflight():
    missing = [p for p in (ACTIONS / "digest_more_like_this.py",
                           ACTIONS / "digest_less_like_this.py", TOOL) if not p.exists()]
    if missing:
        sys.exit("Missing source files (run from the repo, not a copy):\n  "
                 + "\n  ".join(str(m) for m in missing))
    if not API_KEY:
        sys.exit("Set OWUI_API_KEY (Open WebUI > Settings > Account > API Keys).")


def main():
    _preflight()
    install_actions()
    install_tool()
    install_model()
    print("\nDone. Notes:")
    print("  - The two Actions are created AND enabled globally, so their feedback "
          "buttons appear on delivered digests. To scope them to the digest model "
          "instead, turn off 'Global' for each in Admin > Functions.")
    print("  - The Tool/Actions/Pipeline are standalone HTTP clients (no digestcore in "
          "OWUI). Set DIGEST_CORE_URL and DIGEST_TOKEN_PATH on the OWUI/pipelines "
          "containers, share the token-path volume between them, and approve once with "
          "`digest-core auth approve owui`.")


if __name__ == "__main__":
    main()
