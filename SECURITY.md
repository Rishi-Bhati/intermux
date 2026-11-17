# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.2.x (Linux) | Yes |
| 2.1.x (Linux) | No. Upgrade to 2.2.x |
| 2.0.x (Linux) | No. Upgrade to 2.2.x |
| 1.x (Linux) | No. Upgrade to 2.2.x |
| Windows edition (`windows/`) | See [windows/README.md](windows/README.md) |

## Reporting a Vulnerability

Please send every bug report and security concern through the InterMux reporting portal:

**[InterMux reporting portal](https://reportary.onrender.com/p/nWQbq2dDTlsy89u6XBWG2UV0pz9-vQuR_WnRuU7U7bWUMVi5rqRlwDxhs8j_w6r9/)**

How to report:

1. Create a bug report zip. In the GUI, click **Report issue**, then **Save bug report**. From the command line, run `intermux report`. The zip is saved to `~/intermux-reports` by default; use `--out DIR` to choose another folder.
2. Open the portal and upload the zip. In the GUI, **Open reporting portal** opens the page. From the command line, `intermux report --web` opens it.
3. Describe what you did, what you expected, what happened, your distribution, and the InterMux version.

Before you upload, look through the zip. It holds logs and system details. InterMux removes your home path, user name, IP and MAC addresses, URL queries and tokens, but check it for anything private.

This is a small project maintained on a best-effort basis.

## Security Model

InterMux is split into two parts:

- **Unprivileged side.** The GUI and CLI run as your user. They never run root commands themselves.
- **Privileged helper.** Network changes (namespaces, veth pairs, routes, NAT, IP forwarding) are made by a helper started with `pkexec`. Authorization is checked by polkit under the action `org.intermux.run`, and the policy is attached to the installed binary at `/usr/lib/intermux/intermux`.

Protections in the helper:

- **Polkit scope.** The action `org.intermux.run` authorises running the installed binary through `pkexec`, and by default every use requires administrator authentication (`auth_admin`, no cached authorization). With passwordless mode on, members of the `intermux` group are not asked (see [Passwordless mode](#passwordless-mode-opt-in)). As root, the binary refuses every command except `helper` and `watch` (`intermux/__main__.py`), and refuses `watch` when it was started through `pkexec`, so through polkit only the helper can run.
- **Watcher service.** `intermux-watch.service` runs `intermux watch` as root under systemd. It only removes bindings whose namespace has no processes left (the same work as `teardown-idle`) and re-adds dropped policy routes. It takes no input from users and never signals processes.
- **Root only.** The helper refuses to run unless its effective UID is 0. It finds the calling user from `PKEXEC_UID` or `SUDO_UID`.
- **Strict input validation.** Each request must be JSON of at most 1 MiB with only the keys `v`, `action` and `params`, a matching protocol version, and one of the known actions. Unknown parameters are rejected. Interface names must match `^[A-Za-z0-9_.:@-]{1,15}$`. Application paths must be absolute, not end in `/`, not contain `=`, be valid UTF-8 of at most 4096 bytes, and contain no NUL, CR or LF. The running-app policy must be one of `ask`, `close`, `separate`, and flags must be booleans. DNS servers must be one to four public IPv4 addresses (loopback, link-local, multicast, unspecified and reserved addresses are refused). Speed limits must be numbers from 0.064 to 100000 Mbit/s. A batch holds at most 50 operations, and only `bind`, `adopt` and `set_limit` are allowed in it. Adoption ids are 8 to 32 lowercase hexadecimal characters.
- **Whose processes are touched.** The helper takes the acting user from `PKEXEC_UID` or `SUDO_UID`, never from the request, and refuses requests from root. Stopping, restarting, moving or removing an app, removing a namespace, and routing a running app only act on processes owned by that user. Before a running process is moved into its routing group, the helper checks again that it still belongs to that user and is the same process (its start time), because a process id can be reused.
- **State file checks.** Values read back from `/var/lib/intermux/state.json` end up in cgroup, sysctl and nftables paths. Interface and namespace names, adoption slots and the adoption group path (which must be exactly `intermux/adopt-<id>`) are checked when the file is read, and entries that do not match are ignored.
- **firewalld.** When firewalld is running, the helper puts the host end of each namespace link in firewalld's `trusted` zone for the runtime only, and takes it out again before the link is deleted. The link name is checked against `^veth0_[0-9a-f]{8}$` first.
- **Namespace name lookups.** Each namespace gets its own `resolv.conf` and, on hosts that send lookups to systemd-resolved, an `nsswitch.conf` copied from the host's with the `resolve` module removed. Without that, lookups would go to the host's resolver over a Unix socket and ignore the namespace's DNS.
- **VPN connections.** The VPN connections window runs `nmcli` as your user. It does not use the root helper, so NetworkManager's own authorization applies.
- **Environment allowlist.** Only a short list of display, audio, D-Bus and locale variables is passed from your session. Values are checked for length and control characters. Paths such as `XAUTHORITY` and `WAYLAND_DISPLAY` must stay under your home or runtime directory.
- **No shell.** Every external command is an argument list. `shell=True` and `os.system` are not used.
- **Applications run as you.** The helper enters the namespace and then starts the application with `runuser -u <you>` and a clean environment (`env -i`). Applications do not run as root.
- **State lock.** Changes to `/var/lib/intermux/state.json` are serialized with a file lock. A corrupt state file is moved aside, not overwritten.

### Passwordless mode (opt-in)

By default, every network change asks for your password. Passwordless mode is off until you turn it on with `sudo intermux passwordless on`, or answer yes to the question the installer asks. The installer's question defaults to no.

- **What it allows.** Members of the `intermux` group run the helper without a password, and only from a local, active session. Any program running as you can then send the helper any request it accepts, such as binding or stopping apps, changing DNS or speed limits, or routing a running app. Requests still go through the same checks as before.
- **What it does not allow.** It does not give a root shell. The helper accepts only its own requests, and the binary refuses every other command as root. It cannot change its own setting: `passwordless on` and `off` need real sudo and refuse to run through `pkexec`. Other users' routed apps and interfaces stay protected, as described under "Whose processes are touched" above.
- **Sessions.** The polkit rule applies only to local, active sessions. SSH and other inactive sessions still get the password prompt.
- **Files it installs.** The rule is one of these files, depending on the polkit version. Both are owned by root and readable by everyone.
  - `/etc/polkit-1/rules.d/49-intermux-passwordless.rules` on polkit 0.106 and newer (a JavaScript rule).
  - `/etc/polkit-1/localauthority/50-local.d/49-intermux-passwordless.pkla` on polkit 0.105, such as Ubuntu 22.04.

  InterMux also writes a marker file, `/var/lib/intermux/passwordless.json`, and uses the `intermux` group.
- **Turning it off.** Run `sudo intermux passwordless off` for your user, or `sudo intermux passwordless off --all` to remove the rule for everyone. The `--all` form also deletes the `intermux` group if InterMux created it. Uninstalling InterMux turns the mode off too.
- **The helper is the boundary.** With passwordless mode on, no password stands between a program running as you and the helper. Any bug in the helper's input checks or access checks matters more than it does by default, so please report such bugs as described above.

### What InterMux Does Not Protect Against

- **Network namespaces isolate routing, not the application.** A bound app runs as you and can read your files, just like any other program you start. Do not bind software you do not trust.
- **Anyone authorized by polkit can change bindings.** Changes to network setup are allowed for users that polkit authorizes. This is normally administrators, and members of the `intermux` group when passwordless mode is on.
- **DNS and speed limits are shared.** Apps bound to the same interface share one namespace, so its DNS servers and speed limit apply to all of them. A user cannot change them while another user has apps on that interface. Removing the namespace stops only the requesting user's processes and is refused while another user still has processes in it.
- **A shifted app is not fully contained.** "Shift current session" routes the connections an app opens after the move through the chosen interface. Connections it already had open keep their old route until they reconnect. Its name lookups (port 53) deliberately keep the system's normal route, so the DNS servers see those queries. Other IPv6 is rejected so that it cannot leave by another route.
- **VPN files are trusted.** Importing a WireGuard or OpenVPN file hands it to NetworkManager. A VPN configuration can contain routes and scripts, so import only files you trust.
- **The application binary is trusted.** The helper checks that the path is absolute and well formed (no trailing slash, no `=`, no control characters, valid UTF-8, at most 4096 bytes). Whether the file exists and is executable is checked before a request is sent, by the unprivileged CLI and GUI (`resolve_app_path`). The helper does not check the file's existence itself, and the launch fails if the file is missing. InterMux does not verify the program's contents.

### Known limitations

- **Reset removes some rules it did not create.** Reset removes NAT and firewall rules in InterMux's `10.200.x.x` range even when another program made them.
- **IPv4 forwarding is turned on for the whole system.** While InterMux routes apps, it turns on IPv4 forwarding for the whole system.
- **A temporary folder name can be blocked.** Another local user can block your temporary folder name under `/tmp`. The bind then fails with an unsafe-path error.
- **Bound apps are visible to other users.** Every local user can read the list of bound apps in `/var/lib/intermux/state.json`.

### Network Requests

- **Check public IP contacts a public IP service.** The **Check public IP** button (`intermux verify`) sends a request to `https://api.ipify.org` from inside the selected interface's network namespace, so that service sees the public IP address of that connection. Nothing else in InterMux contacts this address.
- **DNS servers are probed.** When InterMux sets up or changes the DNS of an interface, it sends one test query for the root name servers to each configured server from inside the namespace, to put the servers that answer first.
- **The report button does not upload anything.** "Save bug report" only writes a zip to your own disk (default `~/intermux-reports`), and "Open reporting portal" only opens the portal page in your browser, where you upload the zip yourself. No credentials are bundled with InterMux.

If you find a case where any of these protections can be bypassed, please report it as described above.
