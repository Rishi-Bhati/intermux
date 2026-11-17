# InterMux 3 plan

Status of this document: decided. Record the decisions below; do not reopen them here. Changes to a
decision go in a new section with a date.

## Goal

InterMux becomes a persistent desktop app. Today the user opens the window, configures a binding, and
closes it. In 3.0 a background daemon keeps the live model of interfaces, bindings and traffic, and a
tray icon gives the user a compact view and quick actions while the window is closed.

## Pieces

| Piece | Language | Runs as | Role |
|---|---|---|---|
| `intermuxd` | Rust (`rust/intermuxd`) | user; systemd user socket activation | Live model of interfaces, bindings, adoptions and traffic. Socket API and events, as specified in `docs/daemon-protocol.md`. Never changes anything. Never calls the helper. |
| `intermux-tray` | Rust (`rust/intermux-tray`, ksni StatusNotifierItem) | user; started at sign-in by a per-user toggle in its menu | Menu is the compact view: interfaces with status and speed; apps with Stop and Restart; speed-limit presets; Open dashboard; Start at sign-in; Quit. Notifications for route lost and restored, app exited, and a fallback when the desktop has no tray (stock GNOME). |
| Python window and CLI | Python (`intermux/`) | user | Clients of the daemon. One window at a time (`gui.raise`). `intermux events` prints live events. |
| Root helper | Python (`intermux/helper/`) | root, through pkexec | Unchanged. The security boundary: every request is validated in `helper/protocol.py`. |
| Root watcher | Python (`intermux/watch.py`) | root, through systemd | Unchanged. Tears down idle bindings and re-adds routes that the kernel dropped. |

Rules:

- The daemon never calls the helper.
- The tray and the window call the helper from inside the user's session.
- Passwordless mode (2.2.0, opt-in polkit group rule) makes tray actions run without a prompt.

## Releases

- **2.2.0** (shipped 2026-11-18). Passwordless mode and security hardening.
- **3.0.0-beta.1**. The daemon, the tray, and the window as a daemon client (event-driven refresh, single
  instance). `intermux events`. Shared fixtures in `tests/fixtures`, read by both the Python and the Rust
  tests. Packaging: user systemd units; Rust binaries built in ubuntu:22.04, like the bundle; published as
  a GitHub pre-release.
- **3.0.0**. Saved setups: an app, an interface, DNS and a speed limit, started at sign-in. This is
  automatic with passwordless mode and needs one prompt otherwise. Queue preview: namespace, subnet, and
  whether each one is new or reused. Speed limits are kept after "Remove stopped". `intermux status --watch`.
  A CI lane on the oldest supported Python (3.9, the minimum in `pyproject.toml`).
- **3.1**. Flatpak apps and IPv6.
- **Not planned**: porting the helper, the watcher or the window to Rust; moving a live app into another
  namespace; a Windows edition (the Windows edition stays in the 2.x line).

## Decisions

Decisions taken by the maintainer agent. Record them as decided.

- The tray uses its menu as the compact view. It does not use a popup window.
- Passwordless mode ships first, as 2.2.
- Flatpak apps and IPv6 go to 3.1.
- Speed limits are kept after "Remove stopped" in 3.0.

## Status

Checklist for 3.0.0-beta.1. Update it as work lands.

- [ ] Daemon: `intermuxd` implements `docs/daemon-protocol.md` (protocol 1).
- [ ] Python client and window: daemon client, event-driven refresh, single instance, `intermux events`.
- [ ] Tray: `intermux-tray` with the menu, notifications and the start-at-sign-in toggle.
- [ ] Packaging and CI: user units, Rust binaries built in ubuntu:22.04, shared fixtures in both test suites.
- [ ] Docs: `docs/daemon-protocol.md`, `docs/state-format.md`, `docs/HANDOFF.md`, and this plan.
- [ ] Beta release: 3.0.0-beta.1 published as a GitHub pre-release.

## Risks

- **Polkit agent reachability.** A polkit prompt needs a session agent. A process outside a user session
  (a system service, or a daemon with no session agent) cannot show one. Mitigation: only processes in the
  user's session call the helper. The daemon never does.
- **No tray on stock GNOME.** Stock GNOME does not show StatusNotifierItem icons without an extension.
  Fallback: the notifications for route lost and restored and for app exited still reach the user, and the
  window and the CLI still work.
- **Two languages read `state.json`.** Python and Rust must agree on the file format. Mitigation: the
  fixtures in `tests/fixtures` (current format, the format without `uid` and `origins`, and invalid files)
  are loaded by both test suites, and `docs/state-format.md` is the written reference.
