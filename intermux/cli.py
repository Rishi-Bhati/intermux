"""Command-line interface for InterMux.

Read-only commands run as the invoking user. Commands that change system state
are forwarded to the privileged helper through intermux.helper.client.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from intermux.config import VERSION
from intermux.errors import HelperError, InterMuxError, NotFoundError

log = logging.getLogger("intermux.cli")

Handler = Callable[[argparse.Namespace], int]


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser with all user-facing sub-commands."""
    parser = argparse.ArgumentParser(
        prog="intermux",
        description="Route individual desktop applications through a chosen network interface.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("interfaces", help="list network interfaces")
    p.add_argument("--all", action="store_true", help="include interfaces that cannot be bound")

    p = sub.add_parser("apps", help="list desktop applications")
    p.add_argument("--search", metavar="TEXT", help="filter by name, comment or binary")

    p = sub.add_parser("bind", help="run an application inside a namespace bound to an interface")
    p.add_argument("--app", required=True, metavar="APP", help="command name or path of the application")
    p.add_argument("--iface", required=True, metavar="IF", help="network interface to route through")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--separate-profile", action="store_true",
                       help="open a separate profile if the application is already running")
    group.add_argument("--close-existing", action="store_true",
                       help="close running instances of the application and reopen it")
    p.add_argument("--allow-unsupported", action="store_true",
                   help="try to bind an application InterMux does not support")

    sub.add_parser("status", help="show bindings")

    p = sub.add_parser("stop", help="stop the application bound to an interface")
    p.add_argument("--app", required=True, metavar="APP")
    p.add_argument("--iface", required=True, metavar="IF")

    p = sub.add_parser("kill", help="stop every host instance of an application")
    p.add_argument("--app", required=True, metavar="APP")
    p.add_argument("--force", action="store_true", help="kill survivors with SIGKILL")

    p = sub.add_parser("verify", help="print the public IP seen from an interface namespace")
    p.add_argument("--iface", required=True, metavar="IF")

    sub.add_parser("teardown-idle", help="remove plumbing of bindings with no running applications")

    p = sub.add_parser("reset", help="remove everything InterMux created")
    p.add_argument("--kill-apps", action="store_true", help="also stop applications inside InterMux namespaces")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")

    p = sub.add_parser("adopt", help="route an already running application through an interface")
    p.add_argument("--app", required=True, metavar="APP", help="command name or path of the running application")
    p.add_argument("--iface", required=True, metavar="IF", help="network interface to route through")

    p = sub.add_parser("release", help="stop routing an adopted application")
    p.add_argument("id", metavar="ID", help="adoption id shown by adopt and status")

    sub.add_parser("check", help="report missing dependencies")

    p = sub.add_parser("report", help="save a bug report zip, or open the reporting portal")
    p.add_argument("--web", action="store_true", help="open the reporting portal instead of saving a file")
    p.add_argument("--out", metavar="DIR", help="folder for the saved zip (default: ~/intermux-reports)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Run the CLI with argv (default sys.argv[1:]) and return the process exit code."""
    if argv is None:
        argv = sys.argv[1:]
    _configure_logging()
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        if exc.code is None:
            return 0
        return exc.code if isinstance(exc.code, int) else 1
    if not args.command:
        parser.print_help()
        return 1
    try:
        return _HANDLERS[args.command](args)
    except KeyboardInterrupt:
        _error("interrupted")
        return 130
    except InterMuxError as exc:
        log.debug("command failed", exc_info=True)
        _error(str(exc))
        return 1
    except Exception as exc:  # last-resort guard: users never see a traceback
        log.debug("unexpected failure", exc_info=True)
        _error(f"unexpected error: {exc}")
        return 1


def _configure_logging() -> None:
    """Send library logs to stderr; errors only unless INTERMUX_DEBUG is set."""
    level = logging.DEBUG if os.environ.get("INTERMUX_DEBUG") else logging.ERROR
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _info(msg: str) -> None:
    print(f"[+] {msg}")


def _ok(msg: str) -> None:
    print(f"[✓] {msg}")


def _warn(msg: str) -> None:
    print(f"[!] {msg}")


def _error(msg: str) -> None:
    print(f"[X] {msg}", file=sys.stderr)


def _join(items: Sequence[Any]) -> str:
    return ", ".join(str(item) for item in items) or "none"


def _count(value: Any) -> int:
    if isinstance(value, (list, tuple)):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    cells = [[str(h) for h in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    for row in cells:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip() == "yes"
    except EOFError:
        return False


def _resolve_app(app_arg: str, iface: Optional[str] = None) -> str:
    """Resolve an app argument to a path; falls back to recorded bindings if the binary is gone."""
    from intermux import apps, state

    try:
        return apps.resolve_app_path(app_arg)
    except NotFoundError:
        paths = {
            b.app
            for b in state.load_state().bindings
            if (iface is None or b.iface == iface) and app_arg in (b.app, b.app_name)
        }
        if len(paths) == 1:
            return paths.pop()
        raise


def running_choice_action(choice: str) -> Optional[Tuple[str, Any]]:
    """Map a prompt answer to ("policy", value) / ("adopt", None); None when the answer is not offered."""
    return {"1": ("policy", "close"), "2": ("policy", "separate"),
            "3": ("policy", None), "4": ("adopt", None)}.get(choice)


def _ask_running_policy(name: str) -> Optional[Tuple[str, Any]]:
    """Ask what to do about a running application; None means cancel."""
    _warn(f"{name} is already running")
    while True:
        try:
            choice = input("[1] close & reopen  [2] separate profile  [3] cancel  "
                           "[4] shift current session: ").strip()
        except EOFError:
            return None
        action = running_choice_action(choice)
        if action is None:
            print("Enter 1, 2, 3 or 4.")
        elif action == ("policy", None):
            return None
        else:
            return action


def _report_bind(result: Dict[str, Any]) -> None:
    binding = result.get("binding") or {}
    _ok(f"launched {binding.get('app_name', 'application')} (pid {result.get('pid', '?')}) "
        f"on {binding.get('iface', '?')}")
    print(f"    namespace: {binding.get('namespace', '-')}")
    print(f"    subnet:    {binding.get('subnet', '-')}")
    print(f"    profile:   {binding.get('profile_mode', '-')}")
    print(f"    log:       {result.get('log_path', '-')}")
    for warning in result.get("warnings") or []:
        _warn(str(warning))


def _report_stop(data: Dict[str, Any]) -> int:
    stopped = data.get("stopped") or []
    still = data.get("still_running") or []
    if stopped:
        _ok(f"stopped process(es): {_join(stopped)}")
    else:
        _info("no running instance found")
    if still:
        _warn(f"still running: {_join(still)}")
        return 1
    return 0


def _cmd_interfaces(args: argparse.Namespace) -> int:
    from intermux.system import netinfo

    ifaces = netinfo.list_interfaces()
    if not args.all:
        ifaces = [i for i in ifaces if i.bindable]
    if not ifaces:
        _warn("no network interfaces found" if args.all else "no bindable network interfaces found")
        return 0
    rows = [
        [i.name, i.kind, "up" if i.up else "down", ", ".join(i.ipv4_cidrs) or "-",
         ", ".join(i.gateways) or "-", "yes" if i.bindable else "no"]
        for i in ifaces
    ]
    _print_table(["IFACE", "KIND", "STATE", "IPV4", "GATEWAY", "BINDABLE"], rows)
    return 0


def _cmd_apps(args: argparse.Namespace) -> int:
    from intermux.apps import desktop_entries

    entries = desktop_entries.scan_desktop_entries()
    if args.search:
        entries = desktop_entries.search_entries(entries, args.search)
    if not entries:
        _warn("no applications found" + (f" matching '{args.search}'" if args.search else ""))
        return 0
    rows = [[e.name, e.binary or "-", "yes" if e.bindable else f"no ({e.reason})"] for e in entries]
    _print_table(["NAME", "BINARY", "BINDABLE"], rows)
    return 0


def _cmd_bind(args: argparse.Namespace) -> int:
    from intermux import apps
    from intermux.helper import client as helper_client
    from intermux.system import session

    app_path = apps.resolve_app_path(args.app)
    env = session.build_app_env(session.detect_session())
    if args.close_existing:
        policy = "close"
    elif args.separate_profile:
        policy = "separate"
    else:
        policy = "ask"
    name = os.path.basename(app_path)
    _info(f"binding {name} to {args.iface}")
    try:
        result = helper_client.bind(args.iface, app_path, running_policy=policy,
                                    allow_unsupported=args.allow_unsupported, env=env)
    except HelperError as exc:
        if exc.code != "already_running":
            raise
        answer = _ask_running_policy(name)
        if answer is None:
            _warn("cancelled; the running application was left alone")
            return 0
        if answer[0] == "adopt":
            return _adopt_and_report(args.iface, app_path)
        result = helper_client.bind(args.iface, app_path, running_policy=answer[1],
                                    allow_unsupported=args.allow_unsupported, env=env)
    _report_bind(result or {})
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from intermux import state
    from intermux.system import procs

    nat_lines = None
    try:
        from intermux.system import firewall

        nat_lines = firewall.list_intermux_rules()
    except Exception:
        log.debug("nat rule listing failed", exc_info=True)

    st = state.load_state()
    adoptions = getattr(st, "adoptions", None) or []
    if not st.bindings:
        if not adoptions:
            _info("no bindings")
        else:
            _print_adoptions(adoptions)
        return 0
    rows = []
    for b in st.bindings:
        pids = procs.pids_in_netns(b.namespace)
        status = f"running ({len(pids)})" if pids else "stopped"
        try:
            from intermux.system import netns

            ns_up = "yes" if netns.namespace_exists(b.namespace) else "no"
        except Exception:
            log.debug("namespace check failed", exc_info=True)
            ns_up = "unknown"
        if nat_lines is None:
            nat = "unknown"
        else:
            nat = "yes" if any(b.subnet in line.split() for line in nat_lines) else "no"
        rows.append([b.app_name, b.iface, b.subnet, b.profile_mode, status, ns_up, nat, b.log_path or "-"])
    _print_table(["APP", "IFACE", "SUBNET", "PROFILE", "STATUS", "NAMESPACE", "NAT", "LOG"], rows)
    if adoptions:
        print()
        _print_adoptions(adoptions)
    return 0


def _print_adoptions(adoptions: Sequence[Any]) -> None:
    from intermux.system import adopt as adopt_sys

    rows = []
    for a in adoptions:
        try:
            pids = adopt_sys.read_group_pids(a.group)
        except Exception:
            log.debug("adoption group read failed", exc_info=True)
            pids = []
        rows.append([a.id, a.app_name, a.iface, f"live ({len(pids)})" if pids else "idle"])
    _print_table(["ADOPTION", "APP", "IFACE", "STATUS"], rows)


def _cmd_adopt(args: argparse.Namespace) -> int:
    app_path = _resolve_app(args.app)
    return _adopt_and_report(args.iface, app_path)


def _adopt_and_report(iface: str, app_path: str) -> int:
    from intermux.helper import client as helper_client

    _info(f"routing running {os.path.basename(app_path)} through {iface}")
    data = helper_client.call("adopt", {"iface": iface, "app": app_path}) or {}
    adoption = data.get("adoption") or {}
    _ok(f"adopted {adoption.get('app_name', os.path.basename(app_path))} on {adoption.get('iface', iface)}")
    print(f"    id:         {adoption.get('id', '-')}")
    print(f"    moved pids:{_join(data.get('moved_pids') or [])}")
    print(f"    interface:  {adoption.get('iface', iface)}")
    for warning in data.get("warnings") or []:
        _warn(str(warning))
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    from intermux.helper import client as helper_client

    _info(f"releasing adoption {args.id}")
    data = helper_client.call("release_adoption", {"id": args.id}) or {}
    _ok(f"released; moved pids back: {_join(data.get('released') or [])}")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    from intermux.helper import client as helper_client

    app_path = _resolve_app(args.app, args.iface)
    _info(f"stopping {os.path.basename(app_path)} on {args.iface}")
    data = helper_client.call("stop_app", {"iface": args.iface, "app": app_path}) or {}
    return _report_stop(data)


def _cmd_kill(args: argparse.Namespace) -> int:
    from intermux.helper import client as helper_client

    app_path = _resolve_app(args.app)
    _info(f"stopping every instance of {os.path.basename(app_path)}")
    data = helper_client.call("kill_app", {"app": app_path, "force": bool(args.force)}) or {}
    return _report_stop(data)


def _cmd_verify(args: argparse.Namespace) -> int:
    from intermux.helper import client as helper_client

    _info(f"checking public IP via {args.iface}")
    data = helper_client.call("verify", {"iface": args.iface}) or {}
    ip = data.get("public_ip")
    if ip:
        _ok(f"public IP via {args.iface}: {ip}")
        return 0
    _error(f"could not determine public IP via {args.iface}: {data.get('error') or 'unknown error'}")
    return 1


def _cmd_teardown_idle(args: argparse.Namespace) -> int:
    from intermux.helper import client as helper_client

    _info("checking for idle bindings")
    data = helper_client.call("teardown_idle") or {}
    removed = data.get("removed") or []
    torn_down = data.get("torn_down_ifaces") or []
    if not removed and not torn_down:
        _info("nothing to tear down")
        return 0
    for item in removed:
        _ok(f"removed binding {item}")
    for iface in torn_down:
        _ok(f"removed plumbing for {iface}")
    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    from intermux.helper import client as helper_client

    if not args.yes:
        _warn("this removes all InterMux namespaces, veth pairs, routing rules, NAT rules and bindings")
        if args.kill_apps:
            _warn("applications running inside InterMux namespaces will be terminated")
        if not _confirm("Type 'yes' to continue: "):
            _info("aborted; nothing was changed")
            return 1
    _info("removing everything InterMux created")
    data = helper_client.call("reset", {"kill_apps": bool(args.kill_apps)}) or {}
    _ok(f"reset complete: {_count(data.get('namespaces'))} namespace(s), "
        f"{_count(data.get('veths'))} veth pair(s), {_count(data.get('rules'))} routing rule(s), "
        f"{_count(data.get('nat_rules'))} NAT rule(s) removed")
    still = data.get("still_running") or []
    if still:
        _warn(f"still running: {_join(still)}")
    kept = data.get("kept_apps") or []
    if kept:
        # reset keeps a namespace that still has processes, with its routing, so those apps stay online.
        _warn(f"kept the routing of apps that are still running: {_join(kept)}; close them and run "
              "'intermux teardown-idle', or use 'intermux reset --kill-apps'")
    return 1 if still or kept else 0


def _cmd_check(args: argparse.Namespace) -> int:
    from intermux.system import deps

    distro = deps.distro_info()
    _info(f"distribution: {distro.name} (package manager: {distro.package_manager})")
    report = deps.check_dependencies(distro)
    rows = [
        [d.tool, "required" if d.required else "optional", "yes" if d.present else "no", d.purpose]
        for d in report
    ]
    _print_table(["TOOL", "KIND", "PRESENT", "PURPOSE"], rows)
    missing_required = [d for d in report if d.required and not d.present]
    for dep in report:
        if dep.present:
            continue
        if dep.required:
            _error(f"missing {dep.tool}: {dep.install_hint}")
        else:
            _warn(f"optional tool not found: {dep.tool} ({dep.install_hint})")
    if missing_required:
        return 1
    _ok("all required dependencies are present")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    """Save a bug report zip (default) or open the reporting portal (--web)."""
    from pathlib import Path

    from intermux import bugreport, report

    if args.web:
        if report.open_report_page():
            _ok("opened the reporting portal in your browser")
        else:
            _info(f"open this page to report: {report.REPORT_PAGE_URL}")
        return 0
    path = bugreport.write_bug_zip(directory=Path(args.out).expanduser() if args.out else None)
    _ok(f"saved bug report: {path}")
    _info(f"upload this file in the reporting portal: {report.REPORT_PAGE_URL}")
    return 0


_HANDLERS: Dict[str, Handler] = {
    "interfaces": _cmd_interfaces,
    "apps": _cmd_apps,
    "bind": _cmd_bind,
    "status": _cmd_status,
    "stop": _cmd_stop,
    "kill": _cmd_kill,
    "verify": _cmd_verify,
    "teardown-idle": _cmd_teardown_idle,
    "reset": _cmd_reset,
    "adopt": _cmd_adopt,
    "release": _cmd_release,
    "check": _cmd_check,
    "report": _cmd_report,
}
