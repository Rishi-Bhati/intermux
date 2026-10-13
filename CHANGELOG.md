# Changelog

All notable changes to InterMux are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Earlier releases are described in [release_notes.md](release_notes.md).

## [Unreleased]

## [2.0.1] — 2026-10-13

### Fixed

- **Firefox profiles.** A symlinked Firefox profile root (for example one managed by a dotfile tool) no longer stops InterMux from reading the other roots, such as Snap or Flatpak Firefox.

### Changed

- **Security reporting.** SECURITY.md sends every security report and bug report to the Reportary portal, with the bug-report zip from the GUI or `intermux report`.

## [2.0.0] (Linux)

The Linux edition was rewritten as a Python package. The GUI runs without root, and a small polkit-gated helper makes the network changes.

### Added

- **Package layout.** The code now lives in the `intermux` package, with `python3 -m intermux` and an `intermux` command.
- **Interface discovery.** Interfaces are read from `ip -j` output and classified as Wi-Fi, Ethernet, USB, Bluetooth, mobile or virtual.
- **Per-interface namespaces.** Each bound interface gets a network namespace connected to the host by a veth pair.
- **Policy routing.** Each namespace has its own routing table and rule, so its traffic leaves through the assigned interface.
- **NAT and forwarding.** iptables MASQUERADE and FORWARD rules are added for each subnet. IPv4 forwarding is enabled through `/proc`, and the previous value is restored when the last binding is removed.
- **DNS.** Each namespace gets its own resolv.conf: the interface gateway first, then public resolvers. Host resolvers are not used.
- **Application discovery.** Installed desktop applications are listed from `.desktop` files and can be searched. Entries that cannot be bound show the reason.
- **Firefox profiles.** Firefox can reuse your default profile or use a separate profile per interface. Stale profile locks are removed when Firefox is not running.
- **Running instances.** When an app is already running, InterMux can ask whether to close and reopen it, open a separate profile, or cancel.
- **Privileged helper.** Network changes run in a root helper started through `pkexec` (polkit action `org.intermux.run`). Requests use a versioned JSON protocol and are validated before use.
- **Actions.** Bind, stop an app, kill an app, verify the public IP, show status, tear down idle bindings, and reset everything.
- **State file.** Bindings are stored in `/var/lib/intermux/state.json` under a lock. A corrupt file is moved aside.
- **GUI.** A Tk window with an interface list, an application picker, a bindings table with Verify, Stop and Open log, Teardown idle, Reset everything and Force-quit. Keyboard shortcuts are Enter, Delete and F5.
- **CLI.** Commands for `interfaces`, `apps`, `bind`, `status`, `stop`, `kill`, `verify`, `teardown-idle`, `reset` and `check`.
- **Packaging.** A PyInstaller onedir build, `install.sh` and `uninstall.sh`, a polkit policy, a desktop entry, an icon and AppStream metadata. Starting points for Arch, Debian and Fedora packaging are included and are untested.
- **Continuous integration.** Unit tests on every pull request, an opt-in namespace integration job, and a build workflow that publishes a release tarball with a SHA-256 checksum.
- **Tests.** Unit tests cover subnets, state, command handling, process handling, interface parsing, Firefox handling, desktop entries, the helper protocol and the CLI parser.

- **Automatic cleanup.** The `intermux-watch` systemd service removes a binding's namespace, routes and NAT rules when its last app exits. It does a cheap check each pass and only takes the state lock when something is idle. `install.sh` and the distro packages install it.
- **Chromium-based browsers.** Chromium, Chrome, Brave, Vivaldi, Edge and Opera can be bound. A separate profile gets its own data folder per browser and interface.
- **Report issue.** A GUI button and the `intermux report` command save a bug report zip (app logs and system details, with personal data scrubbed on a best-effort basis) to `~/intermux-reports/`, or open the reporting portal (`--web`, `--out DIR`).

### Changed

- **Minimum Python** is now 3.9 (was 3.6).
- **Unprivileged by default.** Only the helper runs as root. Applications launch as your user.
- **Application logs** are written per binding to `~/.local/state/intermux/logs/` (was `~/.local/share/intermux/intermux.log`).
- **Subnets** come from the `10.200.N.0/24` pool, and routing tables use IDs from `1000 + N`.
- **Forwarding** is set through `/proc/sys/net/ipv4/ip_forward`. The `sysctl` tool is still used inside each namespace for the DNS redirect.
- **Privilege elevation** works from the PyInstaller binary, which re-runs itself instead of calling `python3`.
- **DNS in namespaces.** Each namespace uses its interface's gateway first, then `1.1.1.1` and `9.9.9.9`, instead of the host's resolvers.

### Removed

- **Third-party Python packages.** The runtime uses only the standard library.
- **Legacy code.** The old root-level `core/`, `gui/` and `cli.py` were replaced by the `intermux` package.
- **Manual routing configuration.** The table and priority values in `core/router.py` are gone. They are now derived from the subnet index.

### Fixed

- **Uninstall.** `uninstall.sh` sends its reset straight to the helper. Before, it ran `intermux reset` as root, which the root guard refuses, so uninstalling failed unless `--force` was given.
- **Running-app detection for browsers.** "Close and reopen" and "Force-quit" for one Chromium-based browser no longer match the processes of another (for example Brave no longer matches Chrome). Chrome and Edge launchers (`google-chrome-stable`, `microsoft-edge-stable`) are recognised.
- **Reset.** The CLI and GUI now say when reset kept the routing of apps that are still running.

### Security

- Privileged operations are limited to the helper, which runs only through polkit authorization.
- Requests are validated against a fixed schema, and unknown fields are rejected.
- The environment passed to applications is limited to an allowlist.
- No command is run through a shell.
- **Privilege boundary.** The root helper accepts only validated requests from the calling user.
- **Process scoping.** Actions that stop or find applications now apply only to the intended processes.
- **Firewall and routing scoping.** Firewall and routing changes are limited to InterMux's own subnets, tables and rules.
- **File handling for root.** The helper handles user-owned files more safely when running as root.
- **Install ownership.** Installed files are owned by root and cannot be changed by regular users.
- **Uninstall safety.** The uninstaller removes only what InterMux installed.
- **Report client redirects.** The report client no longer follows redirects blindly.

## Previous Releases

- **1.2.0**: Windows 10 and 11 support (SOCKS5 proxy engine, Phase 1)
- **1.1.0**: Firefox per-interface profiles, policy routing and cross-distro hardening
