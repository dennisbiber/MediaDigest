#!/usr/bin/env python3
"""Digest installer — orchestrates the whole deployment.

Order (your spec):
  1. install the core (owns the data volume + the API + the scheduler)
  2. prompt for a front-end interface, install it, create + approve its token
  3. prompt for an LLM engine, install it (container) or configure it (API)
  4. any further phases discovered the same way

Everything is **discovered**, not hard-coded: each option is a directory under
``deploy/<category>/<name>/`` containing a ``bundle.toml`` (and a compose file and/or
a ``setup.py`` hook). Drop in a new directory — a Slack front-end, an LM Studio engine
— and it appears in the menu with no change here. That's the whole point: a new
integration is a new folder, not new installer code.

This script shells out to ``docker``; the docker and prompt calls are injectable so
the flow can be tested without docker. The compose files live in ``deploy/`` and are
the deployment artifacts you verify against a real engine.
"""

from __future__ import annotations

import os
import sys
import time
import tomllib
import subprocess
import importlib.util
from dataclasses import dataclass, field

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(REPO, "deploy")
ENV_FILE = os.path.join(DEPLOY, ".env")
CORE_DIR = os.path.join(DEPLOY, "core")
NETWORK = "digest-net"
CORE_URL = os.environ.get("DIGEST_CORE_URL", "http://localhost:8787")

# Ordered phases. Adding a *category* is one line here; adding an *option* within a
# category needs no code change at all.
PHASES = [
    ("frontends", "front-end interface"),
    ("llm", "LLM engine"),
]


@dataclass
class Bundle:
    key: str
    dir: str
    meta: dict
    @property
    def name(self) -> str:
        return self.meta.get("name", self.key)
    @property
    def description(self) -> str:
        return self.meta.get("description", "")


def discover(category: str) -> list[Bundle]:
    base = os.path.join(DEPLOY, category)
    out = []
    if not os.path.isdir(base):
        return out
    for d in sorted(os.listdir(base)):
        meta_path = os.path.join(base, d, "bundle.toml")
        if os.path.isfile(meta_path):
            with open(meta_path, "rb") as f:
                out.append(Bundle(d, os.path.join(base, d), tomllib.load(f)))
    return out


# ---- injectable side effects (real by default; stubbed in tests) ----
def _real_run(cmd, check=True, env=None):
    return subprocess.run(cmd, check=check, env={**os.environ, **(env or {})})


def _real_ask(prompt, default=""):
    suffix = f" [{default}]" if default else ""
    ans = input(f"{prompt}{suffix}: ").strip()
    return ans or default


def _real_health(url) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/healthz", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _gpu_available() -> bool:
    """True when docker exposes an NVIDIA runtime (i.e. the Container Toolkit is set
    up). GPU is the default whenever this holds; otherwise bundles run on CPU."""
    try:
        out = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"],
                             capture_output=True, text=True, timeout=10)
        return "nvidia" in (out.stdout or "")
    except Exception:  # noqa: BLE001
        return False


@dataclass
class Ctx:
    run: callable = _real_run
    ask: callable = _real_ask
    health: callable = _real_health
    core_url: str = CORE_URL
    env_file: str = ENV_FILE
    core_dir: str = CORE_DIR

    def compose_up(self, bundle_dir: str, extra_files: list[str] | None = None, force: bool = False):
        files = ["-f", os.path.join(bundle_dir, "docker-compose.yml")]
        for ef in (extra_files or []):
            files += ["-f", ef]
        cmd = ["docker", "compose", *files, "--env-file", self.env_file, "up", "-d"]
        if force:
            cmd.append("--force-recreate")   # env-file content changes don't recreate otherwise
        self.run(cmd)

    def core_exec(self, args: list[str]):
        self.run(["docker", "compose", "-f", os.path.join(self.core_dir, "docker-compose.yml"),
                  "exec", "-T", "digest-core", "digest-core", *args])

    def set_core_env(self, key: str, value: str):
        _update_env(self.env_file, key, value)

    def wait_core_healthy(self, tries=30):
        for _ in range(tries):
            if self.health(self.core_url):
                return True
            time.sleep(1)
        return False

    def wait_and_approve(self, name: str, tries=30):
        """Wait for an interface to auto-register, then approve its token."""
        for _ in range(tries):
            try:
                self.core_exec(["auth", "approve", name])
                return True
            except subprocess.CalledProcessError:
                time.sleep(1)
        print(f"  (could not auto-approve '{name}'; approve it later with "
              f"`digest-core auth approve {name}`)")
        return False


def _update_env(env_file: str, key: str, value: str):
    os.makedirs(os.path.dirname(env_file), exist_ok=True)
    lines, found = [], False
    if os.path.exists(env_file):
        with open(env_file) as f:
            lines = f.read().splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    with open(env_file, "w") as f:
        f.write("\n".join(lines) + "\n")


def _choose(ctx: Ctx, label: str, options: list[Bundle]) -> Bundle:
    if len(options) == 1:
        print(f"\n{label}: using the only available option — {options[0].name}")
        return options[0]
    print(f"\nChoose a {label}:")
    for i, o in enumerate(options, 1):
        print(f"  {i}. {o.name}{' — ' + o.description if o.description else ''}")
    while True:
        ans = ctx.ask(f"Enter 1-{len(options)}", "1")
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1]
        print("  please enter a valid number.")


def install_core(ctx: Ctx):
    print("\n[1] Core service (owns the data volume, API, scheduler)")
    if not os.path.exists(ctx.env_file):       # compose env_file must exist
        os.makedirs(os.path.dirname(ctx.env_file), exist_ok=True)
        open(ctx.env_file, "a").close()
    ctx.run(["docker", "network", "create", NETWORK], check=False)  # idempotent
    ctx.compose_up(ctx.core_dir)
    if ctx.wait_core_healthy():
        print("  core is up and healthy.")
    else:
        print("  WARNING: core did not report healthy yet; continuing.")


def install_bundle(ctx: Ctx, bundle: Bundle):
    meta = bundle.meta
    # a bundle can declare core settings it needs (e.g. Ollama's base URL)
    for k, v in (meta.get("core_env") or {}).items():
        ctx.set_core_env(k, v)
    if meta.get("type", "compose") == "compose":
        extra = []
        gpu_overlay = meta.get("gpu_compose")
        if gpu_overlay:
            if _gpu_available():
                print("  GPU detected — enabling GPU passthrough.")
                extra.append(os.path.join(bundle.dir, gpu_overlay))
            else:
                print("  No NVIDIA runtime detected — running on CPU.")
        ctx.compose_up(bundle.dir, extra_files=extra)
    # optional post-up hook: setup.py with def setup(ctx, bundle)
    setup_path = os.path.join(bundle.dir, "setup.py")
    if os.path.isfile(setup_path):
        spec = importlib.util.spec_from_file_location(f"setup_{bundle.key}", setup_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.setup(ctx, bundle)


def main(ctx: Ctx | None = None):
    ctx = ctx or Ctx()
    print("Digest installer")
    install_core(ctx)
    changed_core_env = False
    for n, (category, label) in enumerate(PHASES, start=2):
        options = discover(category)
        if not options:
            print(f"\n[{n}] No {label} bundles found under deploy/{category}/ — skipping.")
            continue
        print(f"\n[{n}] {label.capitalize()}")
        choice = _choose(ctx, label, options)
        if choice.meta.get("core_env"):
            changed_core_env = True
        install_bundle(ctx, choice)
    if changed_core_env:
        print("\nApplying interface/engine settings to the core…")
        ctx.compose_up(ctx.core_dir, force=True)   # force: env-file changes don't recreate otherwise
    print("\nDone. The core owns your data; interfaces talk to it with approved tokens.")
    print("\nTo use the host CLI (admin/diagnostics):")
    print("  pip install -e .")
    dh = ""
    try:
        for ln in open(ctx.env_file):
            if ln.startswith("DIGEST_DATA_HOST="):
                dh = ln.split("=", 1)[1].strip()
    except OSError:
        pass
    print(f"  digest core set --url {CORE_URL}" + (f" --data-dir {dh}" if dh else
          "  --data-dir <the core's DIGEST_DATA_HOST>"))
    print("  digest auth approve cli      # approve the CLI from the host (no docker)")
    print("  digest core status           # confirm reachable + approved")


if __name__ == "__main__":
    main()
