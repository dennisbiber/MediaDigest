"""Post-up hook for the OWUI front-end.

Folds in everything we had to do by hand the first time, so a fresh install is clean:
  1. Detect the machine's LAN IP and set the core's OWUI public URL (the address your
     phone uses for the notification link) — no hand-edited placeholder.
  2. Wait for OWUI to serve, then create the admin account and mint an API key over
     OWUI's own API (first signup becomes admin; falls back to prompting if that path
     isn't available on your OWUI build).
  3. Register the digest Tool/Actions/Model with that key.
  4. Approve the pipeline's auto-registered 'owui' token.
"""

import os
import sys
import json
import time
import socket
import subprocess
import urllib.request
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _tailscale_ip() -> str:
    """The node's Tailscale IPv4 (100.x), if Tailscale is up. This is the address a
    phone on the tailnet uses to reach OWUI, which is what notification links need."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.startswith("100."):
                return line
    except Exception:
        pass
    return ""


def _lan_ip() -> str:
    try:
        out = subprocess.run(["ip", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=5)
        parts = out.stdout.split()
        if "src" in parts:
            return parts[parts.index("src") + 1]
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80)); ip = s.getsockname()[0]; s.close()
        return ip
    except Exception:
        return "localhost"


def _http(method, url, body=None, token=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def _wait_for_owui(base, tries=60):
    for _ in range(tries):
        try:
            urllib.request.urlopen(base.rstrip("/") + "/health", timeout=3).read()
            return True
        except Exception:
            time.sleep(2)
    return False


def _auto_api_key(base, ctx):
    """Create the admin account and mint an API key. Returns the key, or '' to fall back."""
    print("  Creating the OWUI admin account…")
    email = ctx.ask("  admin email", "admin@localhost")
    pw = ctx.ask("  admin password (min 8 chars)", "")
    if len(pw) < 8:
        print("  password too short — skipping automated setup.")
        return ""
    name = email.split("@")[0]
    token = ""
    try:
        _, resp = _http("POST", base.rstrip("/") + "/api/v1/auths/signup",
                        {"name": name, "email": email, "password": pw})
        token = resp.get("token", "")
    except urllib.error.HTTPError:
        try:  # account may already exist -> sign in
            _, resp = _http("POST", base.rstrip("/") + "/api/v1/auths/signin",
                            {"email": email, "password": pw})
            token = resp.get("token", "")
        except Exception as e:
            print(f"  signup/signin failed ({e}); will prompt for a key.")
            return ""
    except Exception as e:
        print(f"  signup failed ({e}); will prompt for a key.")
        return ""
    if not token:
        return ""
    try:
        _, resp = _http("POST", base.rstrip("/") + "/api/v1/auths/api_key", token=token)
        return resp.get("api_key", "")
    except Exception as e:
        print(f"  could not mint an API key automatically ({e}).")
        return ""


def setup(ctx, bundle):
    base = ctx.ask("OWUI URL", "http://localhost:3000")

    # 1) public URL for notification links -> the address your phone actually uses.
    # Prefer a detected Tailscale IP (VPN), fall back to the LAN IP, always confirmable.
    ts, lan = _tailscale_ip(), _lan_ip()
    default_ip = ts or lan
    hint = " (Tailscale detected)" if ts else ""
    addr = ctx.ask(f"address your phone uses to reach OWUI{hint}", default_ip)
    public = f"http://{addr}:3000"
    ctx.set_core_env("DIGEST_OWUI_PUBLIC_URL", public)
    print(f"  notification links will point at {public}")

    # 1b) ntfy is OWUI's notifier (OWUI chats don't push to a phone). Configure the
    # server here so it's set up exactly when OWUI is chosen. The core has a
    # host-gateway alias, so host.docker.internal works for a host-run ntfy.
    ntfy = ctx.ask("ntfy server URL (push notifications)", "http://host.docker.internal:5555")
    ctx.set_core_env("DIGEST_NTFY_BASE_URL", ntfy)

    # 2) wait for OWUI, then get an API key (automated, with manual fallback)
    print("  Waiting for Open WebUI to come up…")
    if not _wait_for_owui(base):
        print("  OWUI didn't respond in time; continuing — register functions later with scripts/install_owui.py")
        return
    key = _auto_api_key(base, ctx)
    if not key:
        key = ctx.ask("  paste an OWUI API key (Settings > Account > API Keys)", "")
    if not key:
        print("  no API key — skipping function registration. Run scripts/install_owui.py later.")
    else:
        base_model = ctx.ask("  tool-capable base model id", "qwen3:14b")
        env = {"OWUI_URL": base, "OWUI_API_KEY": key, "DIGEST_ASSISTANT_BASE_MODEL": base_model}
        ctx.run([sys.executable, os.path.join(REPO, "scripts", "install_owui.py")], check=False, env=env)

    # 3) approve the pipeline's auto-registered token
    print("  Approving the OWUI integration's token with the core…")
    ctx.wait_and_approve("owui")
