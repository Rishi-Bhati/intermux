# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.1.x (Linux) | Yes |
| 2.0.x (Linux) | No. Upgrade to 2.1.x |
| 1.x (Linux) | No. Upgrade to 2.1.x |
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

- **Polkit scope.** The action `org.intermux.run` authorises running the installed binary through `pkexec`, and every use requires administrator authentication (`auth_admin`, no cached authorization). As root, the binary refuses every command except `helper` and `watch` (`intermux/__main__.py`), and refuses `watch` when it was started through `pkexec`, so through polkit only the helper can run.
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

### What InterMux Does Not Protect Against

- **Network namespaces isolate routing, not the application.** A bound app runs as you and can read your files, just like any other program you start. Do not bind software you do not trust.
- **Anyone authorized by polkit can change bindings.** Changes to network setup are allowed for users that polkit authorizes. This is normally administrators.
- **DNS and speed limits are shared.** Apps bound to the same interface share one namespace, so its DNS servers and speed limit apply to all of them, including apps of other users. Removing the namespace stops only the requesting user's processes and is refused while another user still has processes in it.
- **A shifted app is not fully contained.** "Shift current session" routes the connections an app opens after the move through the chosen interface. Connections it already had open keep their old route until they reconnect. Its name lookups (port 53) deliberately keep the system's normal route, so the DNS servers see those queries. Other IPv6 is rejected so that it cannot leave by another route.
- **VPN files are trusted.** Importing a WireGuard or OpenVPN file hands it to NetworkManager. A VPN configuration can contain routes and scripts, so import only files you trust.
- **The application binary is trusted.** The helper checks that the path is absolute and well formed (no trailing slash, no `=`, no control characters, valid UTF-8, at most 4096 bytes). Whether the file exists and is executable is checked before a request is sent, by the unprivileged CLI and GUI (`resolve_app_path`). The helper does not check the file's existence itself, and the launch fails if the file is missing. InterMux does not verify the program's contents.

### Network Requests

- **Check public IP contacts a public IP service.** The **Check public IP** button (`intermux verify`) sends a request to `https://api.ipify.org` from inside the selected interface's network namespace, so that service sees the public IP address of that connection. Nothing else in InterMux contacts this address.
- **DNS servers are probed.** When InterMux sets up or changes the DNS of an interface, it sends one test query for the root name servers to each configured server from inside the namespace, to put the servers that answer first.
- **The report button does not upload anything.** "Save bug report" only writes a zip to your own disk (default `~/intermux-reports`), and "Open reporting portal" only opens the portal page in your browser, where you upload the zip yourself. No credentials are bundled with InterMux.

If you find a case where any of these protections can be bypassed, please report it as described above.
