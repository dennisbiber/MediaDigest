"""Command-line client for the digest core service.

The CLI is an admin / diagnostic / setup tool, not a place digests are rendered.
It talks only to the core's HTTP API (never the database), and a `run` simply asks
the core to deliver to the configured front-end and prints a status line.

Treated like any other interface, it generates its own token on first use, persists
it as a local 0600 secret, and registers it with the core — which won't honor it
until you approve it there (`digest-core auth approve cli`).

    digest core set --url http://localhost:8787 [--user <uuid>]
    digest core show | status
    digest options
    digest register [--ntfy TOPIC] [--tz America/Chicago]
    digest sub add NAME [--adapter ...] [--query ...] [--count N] [--window N]
                        [--hour H] [--dow mon,tue] [--dom '*']
    digest sub list | toggle NAME --on/--off | rm NAME
    digest feedback <adapter__item_id> up|down
    digest run [NAME]          # core delivers to the configured front-end; prints status
"""

from __future__ import annotations

import os
import sys
import argparse
import tomllib

from digestcore.client import (DigestClient, CoreError,
                               admin_pending, admin_list, admin_approve, admin_revoke)
from digestcore.bootstrap import ensure_registered, default_token_path
from digestcore.marker import MARKER_SEP

CLIENT_NAME = "cli"


# ---------------- client config (core URL + default user) ----------------
def _cfg_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "digest", "client.toml")


def _load_cfg() -> dict:
    try:
        with open(_cfg_path(), "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError):
        return {}


def _save_cfg(data: dict) -> str:
    path = _cfg_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    for k, v in data.items():
        if v is None:
            continue
        esc = str(v).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{k} = "{esc}"')
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _core_url(args) -> str:
    return (getattr(args, "core", None) or os.environ.get("DIGEST_CORE_URL")
            or _load_cfg().get("core_url") or "")


def _user(args) -> str:
    return (getattr(args, "user", None) or os.environ.get("DIGEST_USER")
            or _load_cfg().get("user") or "local")


def _data_dir(args) -> str:
    return (getattr(args, "data_dir", None) or os.environ.get("DIGEST_DATA_HOST")
            or _load_cfg().get("data_dir") or "")


def _admin_token(args) -> str:
    """The operator admin secret the core wrote into its data directory. Reading it is
    the proof of local operator access that approving a client requires."""
    tok = os.environ.get("DIGEST_ADMIN_TOKEN")
    if tok:
        return tok
    d = _data_dir(args)
    if not d:
        sys.exit("don't know the core's data directory. Set it once:\n"
                 "  digest core set --data-dir <the core's DIGEST_DATA_HOST path>")
    path = os.path.join(d, "admin.token")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        sys.exit(f"can't read the admin token at {path}.\n"
                 "Make sure --data-dir points at the core's data directory (its "
                 "DIGEST_DATA_HOST) and that the core has started at least once.")


def _client(args) -> DigestClient:
    url = _core_url(args)
    if not url:
        sys.exit("no core URL set. Run: digest core set --url http://<core-host>:8787")
    token = ensure_registered(url, CLIENT_NAME, scope="write")
    return DigestClient(url, token, user_id=_user(args))


def _run(fn):
    """Run a client call, translating core errors into friendly CLI messages."""
    try:
        return fn()
    except CoreError as e:
        if e.status == 403:
            sys.exit("core refused this token (awaiting approval). Approve it with:\n"
                     "  digest auth approve cli")
        if e.status == 401:
            sys.exit("core doesn't recognize this token yet. Re-run `digest core set` "
                     "to re-register, then approve it on the core.")
        if e.status == 0:
            sys.exit(str(e.message))
        sys.exit(f"core error: {e.message}")


# ---------------- commands ----------------
def cmd_core(args):
    if args.action == "show":
        cfg = _load_cfg()
        print(f"core_url = {cfg.get('core_url', '(unset)')}")
        print(f"user     = {cfg.get('user', '(unset)')}")
        print(f"data_dir = {cfg.get('data_dir', '(unset)')}")
        print(f"token    = {default_token_path(CLIENT_NAME)}")
    elif args.action == "set":
        cfg = _load_cfg()
        if args.url:
            cfg["core_url"] = args.url
        if args.user:
            cfg["user"] = args.user
        if getattr(args, "data_dir", None):
            cfg["data_dir"] = args.data_dir
        if not cfg.get("core_url"):
            sys.exit("provide --url http://<core-host>:8787")
        _save_cfg(cfg)
        # generate + register this CLI's token now so the approval prompt appears
        ensure_registered(cfg["core_url"], CLIENT_NAME, scope="write")
        print(f"saved {_cfg_path()}")
        print("registered this CLI's token. Approve it with:\n  digest auth approve cli"
              + ("" if cfg.get("data_dir") else
                 "\n(first: digest core set --data-dir <core's DIGEST_DATA_HOST>)"))
    elif args.action == "status":
        url = _core_url(args)
        if not url:
            sys.exit("no core URL set. Run: digest core set --url http://<core-host>:8787")
        c = _client(args)
        try:
            c.healthz()
        except CoreError as e:
            sys.exit(f"core unreachable at {url}: {e.message}")
        try:
            c.list_subscriptions()
            print(f"core: reachable and this CLI is approved ({url}).")
        except CoreError as e:
            if e.status in (401, 403):
                print(f"core: reachable but this CLI is NOT approved yet ({url}).")
                print("approve it: digest auth approve cli")
            else:
                raise


def cmd_options(args):
    print(_run(lambda: _client(args).options()))


def cmd_register(args):
    r = _run(lambda: _client(args).register_account(ntfy_topic=args.ntfy or "", tz=args.tz or ""))
    print(r.get("message", r))


def cmd_sub(args):
    c = _client(args)
    if args.sub_action == "add":
        r = _run(lambda: c.add_subscription(args.name, adapter=args.adapter, topic_query=args.query or "",
                                            count=args.count, window_days=args.window, hour=args.hour,
                                            day_of_week=args.dow, day_of_month=args.dom))
        print(r.get("message", r))
    elif args.sub_action == "list":
        print(_run(lambda: c.list_subscriptions()).get("message", ""))
    elif args.sub_action == "toggle":
        print(_run(lambda: c.set_enabled(args.name, enabled=args.on)).get("message", ""))
    elif args.sub_action == "rm":
        print(_run(lambda: c.remove(args.name)).get("message", ""))


def cmd_auth(args):
    url = _core_url(args)
    if not url:
        sys.exit("no core URL set. Run: digest core set --url http://<core-host>:8787")
    tok = _admin_token(args)
    try:
        if args.action in ("pending", "list"):
            r = admin_pending(url, tok) if args.action == "pending" else admin_list(url, tok)
            rows = r.get("clients", [])
            if not rows:
                print("none." if args.action == "pending" else "no registered clients.")
                return
            print("name                     scope status")
            for c in rows:
                print(f"  {c['name']:24} {c['scope']:5} {c['status']}")
            if args.action == "pending":
                print("\napprove with: digest auth approve <name>")
        elif args.action == "approve":
            if not args.name:
                sys.exit("usage: digest auth approve <name>")
            print(admin_approve(url, tok, args.name).get("message", ""))
        elif args.action == "revoke":
            if not args.name:
                sys.exit("usage: digest auth revoke <name>")
            print(admin_revoke(url, tok, args.name).get("message", ""))
    except CoreError as e:
        if e.status == 403:
            sys.exit("the core rejected the admin token. Confirm --data-dir points at the "
                     "core's data directory (its DIGEST_DATA_HOST).")
        sys.exit(str(e.message))


def cmd_feedback(args):
    if MARKER_SEP not in args.ref:
        sys.exit(f"reference must look like adapter{MARKER_SEP}item_id")
    adapter, item_id = args.ref.split(MARKER_SEP, 1)
    r = _run(lambda: _client(args).record_feedback(adapter, item_id, args.signal))
    print(r.get("message", r))


def cmd_run(args):
    r = _run(lambda: _client(args).run(args.name or ""))
    print(r.get("message", r))


# ---------------- parser ----------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="digest", description="Digest core client (admin/diagnostic CLI).")
    p.add_argument("--core", help="core URL (overrides config/env)")
    p.add_argument("--user", help="user id (default: config/$DIGEST_USER/'local')")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("core", help="configure/inspect the core connection")
    c.add_argument("action", choices=["set", "show", "status"])
    c.add_argument("--url", help="core base URL, e.g. http://localhost:8787")
    c.add_argument("--user", dest="user", help="default user uuid")
    c.add_argument("--data-dir", dest="data_dir",
                   help="the core's data directory (its DIGEST_DATA_HOST) — lets the CLI "
                        "read the admin token to approve clients")
    c.set_defaults(func=cmd_core)

    a = sub.add_parser("auth", help="approve/inspect client tokens (operator)")
    a.add_argument("action", choices=["pending", "list", "approve", "revoke"])
    a.add_argument("name", nargs="?", help="client name (for approve/revoke)")
    a.set_defaults(func=cmd_auth)

    o = sub.add_parser("options", help="show available digest types")
    o.set_defaults(func=cmd_options)

    r = sub.add_parser("register", help="register/update this account")
    r.add_argument("--ntfy")
    r.add_argument("--tz")
    r.set_defaults(func=cmd_register)

    s = sub.add_parser("sub", help="manage subscriptions")
    ss = s.add_subparsers(dest="sub_action", required=True)
    sa = ss.add_parser("add")
    sa.add_argument("name")
    sa.add_argument("--adapter", default="arxiv_hf")
    sa.add_argument("--query", default="")
    sa.add_argument("--count", type=int)
    sa.add_argument("--window", type=int)
    sa.add_argument("--hour", type=int)
    sa.add_argument("--dow")
    sa.add_argument("--dom")
    ss.add_parser("list")
    st = ss.add_parser("toggle")
    st.add_argument("name")
    g = st.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", dest="on", action="store_true")
    g.add_argument("--off", dest="on", action="store_false")
    sr = ss.add_parser("rm")
    sr.add_argument("name")
    s.set_defaults(func=cmd_sub)

    f = sub.add_parser("feedback", help="rate a delivered item")
    f.add_argument("ref", help="adapter__item_id")
    f.add_argument("signal", choices=["up", "down", "save", "mute"])
    f.set_defaults(func=cmd_feedback)

    rn = sub.add_parser("run", help="ask the core to run + deliver a digest now")
    rn.add_argument("name", nargs="?", help="optional subscription name")
    rn.set_defaults(func=cmd_run)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
