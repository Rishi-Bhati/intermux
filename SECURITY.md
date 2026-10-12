# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2.0.x (Linux) | Yes |
| 1.x (Linux) | No. Upgrade to 2.0.x |
| Windows edition (`windows/`) | See [windows/README.md](windows/README.md) |

## Reporting a Vulnerability

Please report security problems privately. Do not use a public issue or the public reporting portal for them, especially anything that could let an unprivileged user gain root or run commands as another user.

Report privately through GitHub security advisories:

1. Go to https://github.com/Rishi-Bhati/intermux/security/advisories/new
2. Describe the problem, the steps to reproduce it, the affected version, and your distribution.

This is a small project maintained on a best-effort basis. Confirmed issues are fixed through the private advisory before public disclosure.

## Security Model

InterMux is split into two parts:

- **Unprivileged side.** The GUI and CLI run as your user. They never run root commands themselves.
- **Privileged helper.** Network changes (namespaces, veth pairs, routes, NAT, IP forwarding) are made by a helper started with `pkexec`. Authorization is checked by polkit under the action `org.intermux.run`, and the policy is attached to the installed binary at `/usr/lib/intermux/intermux`.

Protections in the helper:

- **Polkit scope.** The action `org.intermux.run` authorises running the installed binary through `pkexec`, and every use requires administrator authentication (`auth_admin`, no cached authorization). The binary refusing every command other than `helper` when run as root is planned and not yet implemented (`intermux/__main__.py`), so the polkit action should be treated as authorising the whole binary.
- **Root only.** The helper refuses to run unless its effective UID is 0. It finds the calling user from `PKEXEC_UID` or `SUDO_UID`.
- **Strict input validation.** Each request must be JSON of at most 1 MiB with only the keys `v`, `action` and `params`, a matching protocol version, and one of the known actions. Unknown parameters are rejected. Interface names must match `^[A-Za-z0-9_.:@-]{1,15}$`. Application paths must be absolute, not end in `/`, be at most 4096 characters, and contain no NUL, CR or LF. The running-app policy must be one of `ask`, `close`, `separate`, and flags must be booleans.
- **Environment allowlist.** Only a short list of display, audio, D-Bus and locale variables is passed from your session. Values are checked for length and control characters. Paths such as `XAUTHORITY` and `WAYLAND_DISPLAY` must stay under your home or runtime directory.
- **No shell.** Every external command is an argument list. `shell=True` and `os.system` are not used.
- **Applications run as you.** The helper enters the namespace and then starts the application with `runuser -u <you>` and a clean environment (`env -i`). Applications do not run as root.
- **State lock.** Changes to `/var/lib/intermux/state.json` are serialized with a file lock. A corrupt state file is moved aside, not overwritten.

### What InterMux Does Not Protect Against

- **Network namespaces isolate routing, not the application.** A bound app runs as you and can read your files, just like any other program you start. Do not bind software you do not trust.
- **Anyone authorized by polkit can change bindings.** Changes to network setup are allowed for users that polkit authorizes. This is normally administrators.
- **The application binary is trusted.** The helper checks that the path is absolute and well formed (no trailing slash, no control characters). Whether the file exists is checked before a request is sent, by the unprivileged CLI and GUI (`resolve_app_path`). The helper does not check the file's existence itself, and the launch fails if the file is missing. InterMux does not verify the program's contents.

### Network Requests

- **Verify contacts a public IP service.** The Verify action sends a request to `https://api.ipify.org` from inside the selected interface's network namespace, so that service sees the public IP address of that connection. Nothing else in InterMux contacts this address.
- **The report button does not upload anything.** "Save bug report" only writes a zip to your own disk (default `~/intermux-reports`), and "Open reporting portal" only opens the portal page in your browser, where you upload the zip yourself. No credentials are bundled with InterMux. `intermux/report.py` has an optional API submit function that reads a token from `INTERMUX_REPORTARY_TOKEN` and refuses redirects, but the GUI does not call it.

If you find a case where any of these protections can be bypassed, please report it as described above.
