"""The core HTTP service — standard library only.

Wraps the existing front-end-agnostic services (Subscription/Feedback/Profile) as a
small JSON API and enforces the token handshake from ``auth.py`` on every route.
This is the single owner of the database; interfaces talk to it and never open the
file themselves.

Routes (write = needs a 'write'-scope approved token; read = any approved token):
  GET  /healthz                         (no auth) liveness
  POST /auth/register                   (no auth) {name, token, scope} -> pending
  GET  /options                         read   available digest types
  GET  /account?user_id=                read
  POST /account                         write  {user_id, owui_token?, ntfy_topic?, tz?}
  GET  /subscriptions?user_id=          read
  POST /subscriptions                   write  {user_id, name, adapter, ...}
  POST /subscriptions/<name>/enabled    write  {user_id, enabled}
  DELETE /subscriptions/<name>?user_id= write
  POST /feedback                        write  {user_id, adapter, item_id, signal, ...}
  POST /feedback/from_text              write  {user_id, text, signal}
"""

from __future__ import annotations

import os
import json
import time
import hmac
import secrets
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from digestcore.config import Config
from digestcore.db import open_db
from digestcore.sources import seed_runtime_files
from digestcore.subscriptions import SubscriptionService
from digestcore.feedback import FeedbackService
from digestcore.runner import DigestRunner
from digestcore.delivery import OwuiChatSink, NtfyNotifier, CallbackSink, NullNotifier
from digestcore.service.auth import ClientRegistry


def build_sink(config: Config):
    """The configured front-end the core delivers to. There is no 'deliver to the
    caller' path — a run always lands in the configured interface."""
    name = (getattr(config, "DELIVERY_SINK", "owui") or "owui").lower()
    if name in ("none", "null"):
        return CallbackSink(lambda sub, items: None)
    # "owui" today; slack/discord sinks slot in here as they're built.
    return OwuiChatSink(config)


def build_notifier(config: Config):
    """How the user gets pinged that a digest is ready. This is the *front-end's*
    concern, not the core's: OWUI chats don't push to a phone, so the OWUI bundle pairs
    itself with the ntfy notifier; a Slack/Discord front-end's own post is the
    notification, so it selects 'none'. Selected via DIGEST_NOTIFIER, set by the bundle."""
    name = (getattr(config, "NOTIFIER", "none") or "none").lower()
    if name == "ntfy":
        return NtfyNotifier(config)
    return NullNotifier()


def build_runner(config: Config, db) -> DigestRunner:
    return DigestRunner(config, db, sink=build_sink(config), notifier=build_notifier(config))


def admin_token_path(config: Config) -> str:
    return os.path.join(config.DATA_DIR, "admin.token")


def ensure_admin_token(config: Config) -> str:
    """Create (once) the operator's admin secret in the core's data directory and
    return it. The data dir is the core's host bind mount, so being able to read this
    file is exactly 'has local operator access' — which is what approving a client
    should require. Interface containers never mount this directory, so they can't read
    it. World-readable (0644) because the data dir itself is the boundary, and on a
    single-host deploy the core often writes as root while the CLI runs as you."""
    path = admin_token_path(config)
    try:
        with open(path) as f:
            tok = f.read().strip()
            if tok:
                return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(tok)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass
    return tok


def _ser(obj):
    """Serialize a service dataclass result to a plain dict; pass dicts through."""
    if is_dataclass(obj):
        return asdict(obj)
    return obj


class _Ctx:
    """Per-request bundle: a fresh DB connection and the services over it."""
    def __init__(self, config: Config):
        self.config = config
        self.db = open_db(config.DB_PATH)
        self.subs = SubscriptionService(self.db)
        self.feedback = FeedbackService(self.db)
        self.registry = ClientRegistry(self.db)

    def close(self):
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass


class Handler(BaseHTTPRequestHandler):
    server_version = "digestcore/1.0"

    # -- plumbing --
    def log_message(self, fmt, *args):  # quieter logs
        pass

    def _send(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def _token(self) -> str:
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else ""

    # -- entry points --
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # -- routing --
    def _dispatch(self, method: str):
        parsed = urlparse(self.path)
        parts = [unquote(p) for p in parsed.path.split("/") if p != ""]
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        cfg = self.server.config

        # unauthenticated routes
        if method == "GET" and parts == ["healthz"]:
            return self._send(200, {"status": "ok", "ts": int(time.time())})
        if method == "POST" and parts == ["auth", "register"]:
            return self._register(cfg)

        # admin routes — gated by the operator's admin token (read from the core's data
        # dir), not by a client token. This is how the host CLI approves without docker.
        if parts and parts[0] == "admin":
            return self._admin(method, parts, cfg)

        # everything else needs an approved token of the right scope
        need_write = method in ("POST", "DELETE")
        ctx = _Ctx(cfg)
        try:
            v = ctx.registry.verify(self._token(), need_write=need_write)
            if not v.ok:
                code = 401 if v.reason in ("missing token", "unknown token") else 403
                return self._send(code, {"ok": False, "error": v.reason})
            self._route(method, parts, query, ctx)
        finally:
            ctx.close()

    def _register(self, cfg):
        b = self._body()
        ctx = _Ctx(cfg)
        try:
            try:
                c = ctx.registry.register(b.get("name", ""), b.get("token", ""),
                                          b.get("scope", "write"))
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            if c.status == "pending":
                # the 'ask the user' moment — surfaced in the core's logs for the admin
                print(f"[auth] '{c.name}' requests a {c.scope} token. Approve with: "
                      f"digest-core auth approve {c.name}", flush=True)
            return self._send(202 if c.status == "pending" else 200,
                             {"ok": True, "name": c.name, "status": c.status, "scope": c.scope})
        finally:
            ctx.close()

    def _admin(self, method, parts, cfg):
        # verify the admin token (constant-time) against the file in the data dir
        want = ensure_admin_token(cfg)
        got = self._token()
        if not got or not hmac.compare_digest(got, want):
            return self._send(403, {"ok": False, "error": "admin token required"})
        ctx = _Ctx(cfg)
        try:
            reg = ctx.registry
            if method == "GET" and parts == ["admin", "clients"]:
                return self._send(200, {"ok": True, "clients": [asdict(c) for c in reg.list()]})
            if method == "GET" and parts == ["admin", "pending"]:
                return self._send(200, {"ok": True, "clients": [asdict(c) for c in reg.pending()]})
            if method == "POST" and parts == ["admin", "approve"]:
                name = self._body().get("name", "")
                ok = reg.approve(name)
                return self._send(200, {"ok": ok, "name": name,
                                        "message": f"approved '{name}'." if ok else
                                        f"nothing to approve for '{name}'."})
            if method == "POST" and parts == ["admin", "revoke"]:
                name = self._body().get("name", "")
                ok = reg.revoke(name)
                return self._send(200, {"ok": ok, "name": name,
                                        "message": f"revoked '{name}'." if ok else
                                        f"no client named '{name}'."})
            return self._send(404, {"ok": False, "error": "no such admin route"})
        finally:
            ctx.close()

    def _route(self, method, parts, query, ctx):
        # /options
        if method == "GET" and parts == ["options"]:
            return self._send(200, {"ok": True, "options": ctx.subs.describe_options()})

        # /account
        if parts == ["account"]:
            if method == "GET":
                acct = ctx.subs.get_account(query.get("user_id", ""))
                if not acct:
                    return self._send(404, {"ok": False, "error": "not registered"})
                return self._send(200, {"ok": True, **_ser(acct)})
            if method == "POST":
                b = self._body()
                r = ctx.subs.register_user(b.get("user_id", ""), owui_token=b.get("owui_token", ""),
                                           ntfy_topic=b.get("ntfy_topic", ""), tz=b.get("tz", ""))
                return self._send(200 if r.ok else 400, _ser(r))

        # /subscriptions and sub-paths
        if parts and parts[0] == "subscriptions":
            if len(parts) == 1 and method == "GET":
                return self._send(200, _ser(ctx.subs.list_subscriptions(query.get("user_id", ""))))
            if len(parts) == 1 and method == "POST":
                b = self._body()
                r = ctx.subs.add_subscription(
                    b.get("user_id", ""), b.get("name", ""), adapter=b.get("adapter", "arxiv_hf"),
                    topic_query=b.get("topic_query", ""), count=b.get("count"),
                    window_days=b.get("window_days"), hour=b.get("hour"),
                    day_of_week=b.get("day_of_week"), day_of_month=b.get("day_of_month"))
                return self._send(200 if r.ok else 400, _ser(r))
            if len(parts) == 3 and parts[2] == "enabled" and method == "POST":
                b = self._body()
                r = ctx.subs.set_enabled(b.get("user_id", ""), parts[1], bool(b.get("enabled", True)))
                return self._send(200 if r.ok else 404, _ser(r))
            if len(parts) == 2 and method == "DELETE":
                r = ctx.subs.remove(query.get("user_id", ""), parts[1])
                return self._send(200 if r.ok else 404, _ser(r))

        # /feedback
        if parts == ["feedback"] and method == "POST":
            b = self._body()
            r = ctx.feedback.record(b.get("user_id", ""), b.get("adapter", ""), b.get("item_id", ""),
                                    b.get("signal", ""), title=b.get("title", ""), url=b.get("url", ""))
            return self._send(200 if r.ok else 400, _ser(r))
        if parts == ["feedback", "from_text"] and method == "POST":
            b = self._body()
            r = ctx.feedback.record_from_text(b.get("user_id", ""), b.get("text", ""), b.get("signal", ""))
            return self._send(200 if r.ok else 400, _ser(r))

        # /run  -> build + deliver to the configured front-end, return a status report only
        if parts == ["run"] and method == "POST":
            b = self._body()
            runner = build_runner(ctx.config, ctx.db)
            name = b.get("name")
            report = runner.run_named(name) if name else runner.run_all()
            return self._send(200, {"ok": True, "message": report.message,
                                    "runs": [{"name": r.name, "count": r.count,
                                              "error": r.error, "retry": r.retry,
                                              "note": r.note}
                                             for r in report.runs],
                                    "health": report.health})

        self._send(404, {"ok": False, "error": "no such route"})


def build_server(config: Config, host: str = "0.0.0.0", port: int = 8787) -> ThreadingHTTPServer:
    config.apply_env()
    seed_runtime_files(config.DATA_DIR)
    open_db(config.DB_PATH).close()  # ensure schema exists before first request
    ensure_admin_token(config)       # operator's admin secret in the data dir
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.config = config
    return httpd
