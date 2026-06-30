"""Core service entry point: `digest-core`.

  digest-core serve [--host 0.0.0.0] [--port 8787]
  digest-core auth pending | list
  digest-core auth approve <name>
  digest-core auth revoke  <name>

`serve` runs the HTTP API (the data owner). The `auth` commands run *locally inside
the core container* with direct DB access — no token needed — and are how you, the
human, approve a registration the core flagged. This is the approval gate: an
interface's token does nothing until you approve it here.
"""

from __future__ import annotations

import sys
import time
import argparse

from digestcore.config import Config
from digestcore.db import open_db
from digestcore.service.auth import ClientRegistry
from digestcore.service.server import build_server


def cmd_serve(args):
    cfg = Config.load(args.config)
    httpd = build_server(cfg, host=args.host, port=args.port)
    # The scheduler lives here now (retiring the OWUI pipeline's in-process one). It
    # gets its own DB connection + runner and ticks every minute.
    from digestcore.scheduler import MinuteScheduler
    from digestcore.service.server import build_runner
    sched = None
    if not args.no_scheduler:
        sdb = open_db(cfg.DB_PATH)
        sched = MinuteScheduler(build_runner(cfg, sdb).tick)
        sched.start()
    print(f"digest-core serving on {args.host}:{args.port}  (db={cfg.DB_PATH}, "
          f"scheduler={'off' if args.no_scheduler else 'on'}, deliver={cfg.DELIVERY_SINK})",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if sched:
            sched.stop()
        httpd.shutdown()


def _registry(args) -> ClientRegistry:
    cfg = Config.load(args.config)
    return ClientRegistry(open_db(cfg.DB_PATH))


def _fmt(c) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(c.created_at)) if c.created_at else "?"
    return f"  {c.name:24} {c.scope:5} {c.status:9} requested {when}"


def cmd_auth(args):
    reg = _registry(args)
    if args.action in ("pending", "list"):
        rows = reg.pending() if args.action == "pending" else reg.list()
        if not rows:
            print("none." if args.action == "pending" else "no registered clients.")
            return
        print("name                     scope status    requested")
        for c in rows:
            print(_fmt(c))
        if args.action == "pending":
            print("\napprove with: digest-core auth approve <name>")
    elif args.action == "approve":
        ok = reg.approve(args.name)
        print(f"approved '{args.name}'." if ok else f"nothing to approve for '{args.name}'.")
    elif args.action == "revoke":
        ok = reg.revoke(args.name)
        print(f"revoked '{args.name}'." if ok else f"no client named '{args.name}'.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="digest-core", description="Digest core service.")
    p.add_argument("--config", help="path to a TOML config file")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the core HTTP API")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--no-scheduler", action="store_true",
                   help="serve the API without the background scheduler")
    s.set_defaults(func=cmd_serve)

    a = sub.add_parser("auth", help="approve or inspect client tokens (local admin)")
    a.add_argument("action", choices=["pending", "list", "approve", "revoke"])
    a.add_argument("name", nargs="?", help="client name (for approve/revoke)")
    a.set_defaults(func=cmd_auth)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "action", None) in ("approve", "revoke") and not args.name:
        sys.exit(f"'{args.action}' needs a client name")
    args.func(args)


if __name__ == "__main__":
    main()
