# InterMux handoff

For the next agent or engineer who takes over the project. The facts here were checked against the code on
`main` (release 2.2.0) and the `feat/v3-*` branches when this file was written. Update the file when a release,
a directory, a command or a branch changes. The v3 status checklist is in `docs/v3-plan.md`.

## What InterMux is

InterMux routes individual desktop applications through a network interface that the user chooses on Linux.
Each bound application runs in its own network namespace, connected to the host by a veth pair, and policy
routing sends that namespace out through the chosen interface. A root helper, started through pkexec, makes the
network changes, and the applications run as the user who started them. Release 2.2.0 is the current Linux
edition; the 3.0 work (a background daemon and a tray icon) is in progress.

## Repository map

Python package `intermux/` (standard library only, Python 3.9 or newer):

- `__main__.py`: dispatch to the window, the helper, the watcher or the CLI. It holds the root guard.
- `cli.py`: the `intermux` commands.
- `engine.py`: the actions (bind, stop, adopt, release, move, restart, reset and others). Callers hold the
  state lock.
- `config.py`: version, paths, the subnet, table and priority bases, and the environment allowlist.
- `state.py`: the `state.json` model, loader, validation, atomic save and lock. See `docs/state-format.md`.
- `watch.py`: the root watcher loop.
- `dnsconf.py`, `subnets.py`, `bugreport.py`, `report.py`: DNS presets and checks, subnet pool, bug report.
- `helper/protocol.py`: the list of actions (`ACTIONS`) and the validation of every parameter. This is the
  security boundary. `helper/server.py` runs one request as root. `helper/client.py` is the unprivileged side
  that calls pkexec.
- `system/`: `netns.py` (namespaces and veth pairs), `firewall.py` (NAT and firewalld), `routing.py` (policy
  routes), `adopt.py` (routing running apps through cgroup and nftables), `nm.py` (NetworkManager),
  `passwordless.py` (the opt-in mode), `safefs.py` (file access under a user's home), `shaping.py` (tc limits),
  `session.py` (user lookup), `procs.py`, `netinfo.py`, `traffic.py`, `commands.py` (runs argument lists only),
  `deps.py`.
- `apps/`: desktop entries, Firefox and Chromium-family handling.
- `ui/`: the Tk window (`app.py`) and its dialogs, graphs, picker, theme and worker.

Other directories:

- `rust/`: the 3.0 Rust code. `rust/intermux-common` (protocol types, state model, shared parsing),
  `rust/intermuxd` (the user daemon), and `rust/intermux-tray` (planned; ksni tray). This directory is not on
  `main` yet. It is being written on `feat/v3-daemon`.
- `tests/`: the unittest suite. `test_netns_integration.py` is the opt-in root test. `tests/fixtures/` is planned
  for 3.0.0-beta.1 and is not in the tree yet.
- `packaging/linux/`: the polkit policy (`org.intermux.policy`), desktop entry, icon, AppStream metadata,
  `intermux-watch.service` and its preset, and `README-install.txt` (shipped in the release tarball).
- `packaging/arch/`, `packaging/debian/`, `packaging/fedora/`: distribution recipes. They are starting points and
  are not published to any repository.
- `intermux_linux.spec`: the PyInstaller spec for the Linux bundle.
- `install.sh`, `uninstall.sh`: the system installer and remover, run from the release tarball or a source tree.
- `get/`: the one-line installers (`install.sh` for Linux, `install.ps1` for Windows).
- `.github/workflows/`: `ci.yml` (unit tests on Python 3.9 and 3.12, the netns integration test, packaging
  lint), `build_linux.yml` (builds the bundle and attaches it to a published release), `build_windows.yml`.
- `windows/`: the Windows edition, a separate code base. It stays in the 2.x line.
- `docs/`: `daemon-protocol.md`, `state-format.md`, `v3-plan.md`, `HANDOFF.md`, `fork-feature-parity.md`, and
  `media/` (the demo video).
- `website/`: the project website. It is not part of the Linux code.
- `promo/video/`: the source of the demo video.
- Top level: `README.md`, `CHANGELOG.md`, `release_notes.md`, `SECURITY.md`, `CONTRIBUTING.md`, `pyproject.toml`.

## How to run and test

- Python unit tests (no root): `xvfb-run -a python3 -m unittest discover -s tests -t .`. CI runs the same
  discovery on Python 3.9 and 3.12 without `xvfb-run`.
- Byte-compile check, as CI does: `python3 -m compileall -q intermux tests`.
- Rust (once `rust/` is on the branch): `cd rust && cargo test`.
- Window: `python3 -m intermux`, as your normal user. The root guard refuses to start the window as root.
- CLI: `python3 -m intermux <command>`, as your normal user. `python3 -m intermux --help` lists the commands.
- Helper: never run it by hand except through pkexec or sudo. Both set `PKEXEC_UID` or `SUDO_UID`, and the helper
  finds the requesting user from those variables. An action that acts for a user is refused without them.
- Namespace integration test. It changes networking on the machine it runs on. Run it only in a virtual machine
  or another disposable system:
  `sudo -E env INTERMUX_INTEGRATION=1 python3 -m unittest tests.test_netns_integration -v`.
- Lint, as CI does: `bash -n install.sh`, `bash -n uninstall.sh`, `sh -n packaging/debian/postinst`,
  `sh -n get/install.sh`, `shellcheck -s sh get/install.sh` (the one-line installer must stay POSIX), and
  `desktop-file-validate packaging/linux/intermux.desktop`.

## Privilege model

- Unprivileged: the window, the CLI, and in 3.0 the daemon and the tray. None of them runs a root command.
- Root helper: `intermux helper`, started by `pkexec /usr/lib/intermux/intermux helper`. It reads one JSON
  request from stdin. The request must be at most 1 MiB, have only the keys `v`, `action` and `params`, and use
  a known action and known parameters (`helper/protocol.py`). The helper takes the acting user from
  `PKEXEC_UID` or `SUDO_UID`. It never takes the user from the request.
- Polkit action `org.intermux.run` (`packaging/linux/org.intermux.policy`). It requires administrator
  authentication for each use, by default. The action is annotated on the installed binary
  `/usr/lib/intermux/intermux`.
- Passwordless mode (2.2.0, opt-in). `sudo intermux passwordless on` adds the user to the `intermux` group and
  installs a polkit rule that lets members run the helper without a password from a local, active session.
  The rule is `/etc/polkit-1/rules.d/49-intermux-passwordless.rules` on polkit 0.106 and newer, and
  `/etc/polkit-1/localauthority/50-local.d/49-intermux-passwordless.pkla` on polkit 0.105 (Ubuntu 22.04). The
  marker is `/var/lib/intermux/passwordless.json`. `passwordless` refuses to run through pkexec.
- Why root may run only `helper`, `watch` and `passwordless`: polkit authorises the path of the whole installed
  binary, not one command. Without the guard, a pkexec call to the binary could run any command as root.
  `__main__.py` refuses every other command as root. `watch` refuses to start through pkexec, because it is a
  systemd service.
- pkexec or sudo. With pkexec, polkit checks the request before the helper runs. The application uses pkexec for
  every network change, unless passwordless mode is on. With sudo, the administrator runs installation,
  `passwordless on|off`, and manual debugging. The helper accepts either to identify the user, but `passwordless`
  refuses pkexec, so only sudo can change that setting.
- 3.0 plan: the tray and the window call the helper from inside the user's session. The daemon never calls the
  helper. See `docs/v3-plan.md`.

## How releases are made

This is the sequence used for 2.1.1 (release commit `7481ce6`, checksum commit `e135858`) and 2.2.0 (release
commit `3179a48`, fixes after it, tag `linux-v2.2.0` on `474ba9c`, checksum commit `93a895e`). Use the same steps for
the next release. Replace `X.Y.Z` with the version.

1. Version bump. Change the version in these places:
   - `pyproject.toml`: `version`.
   - `intermux/config.py`: `VERSION`.
   - `packaging/debian/changelog`: a new top entry.
   - `packaging/fedora/intermux.spec`: `Version`, `Release: 1`, and a new `%changelog` entry.
   - `packaging/linux/intermux.metainfo.xml`: a new `<release version="X.Y.Z" date="YYYY-MM-DD"/>` line.
   - `packaging/arch/PKGBUILD`: `pkgver`.
   - `README.md`: the line "Version X.Y.Z is the Linux edition".
2. Notes. In `CHANGELOG.md`, move the entries from `[Unreleased]` into a new `## [X.Y.Z] - YYYY-MM-DD` section. Add a
   `## vX.Y.Z - title` section at the top of `release_notes.md`. Update the supported-versions table in
   `SECURITY.md` when the supported releases change (2.2.0 did this).
3. Commit the release with a message such as `release: X.Y.Z`, on a branch that is merged to `main` (2.2.0 used
   `feat/2.2`). Fixes found while testing the release go in before the tag is made.
4. Build the bundle in an ubuntu:22.04 container. Never build the release bundle on the Arch host: the host ships
   a newer libz that does not run on glibc 2.35. The steps are those of `.github/workflows/build_linux.yml`:
   install `python3-tk`, run the unit tests, install `pyinstaller==6.20.0`, run `pyinstaller intermux_linux.spec
   --noconfirm`, stage the install layout, create the tree checksum, and build the tarball and its `.sha256`.
   The workflow uses Python 3.11. The ubuntu:22.04 archive ships Python 3.10, so get 3.11 from another source (for
   example the deadsnakes PPA) and record the version used.
5. Test the install. Unpack the tarball in ubuntu:22.04 and debian:12 containers and run `install.sh` as root.
   Check that the bundle runs and that `uninstall.sh --force` removes it.
6. Test polkit end to end in Docker on ubuntu:22.04, debian:12, fedora:41 and archlinux:latest. The harness
   (`polkit-e2e.sh` and `run-all.sh`) is not in the repository yet. At the time of writing it exists only in the
   scratchpad, which is temporary. Add it to the repository before relying on it again.
7. Push `main`. Then create the release and the tag from `main`. Write the notes section to a temporary file outside
   the repository:
   ```
   NOTES="$(mktemp)"
   awk '/^## vX\.Y\.Z /{f=1;next} /^## /{f=0} f' release_notes.md > "$NOTES"
   gh release create linux-vX.Y.Z --target main --notes-file "$NOTES"
   ```
   Replace `X.Y.Z` in both places. Add `--prerelease` for a beta.
8. `build_linux.yml` runs when the release is published. It builds on ubuntu-22.04 and attaches
   `intermux-linux-x86_64.tar.gz` and `intermux-linux-x86_64.tar.gz.sha256` to the release.
9. Verify the published asset. Download it into an empty directory with
   `gh release download linux-vX.Y.Z -p 'intermux-linux-x86_64.tar.gz*'`, then run `sha256sum -c` on the `.sha256`
   file. Check that both files are on the release page. Then run the install test (step 5) on the downloaded
   tarball, not on a local build.
10. Arch checksum. Compute the SHA-256 of the tag's source tarball,
    `https://github.com/Rishi-Bhati/intermux/archive/refs/tags/linux-vX.Y.Z.tar.gz`, or run `updpkgsums` in
    `packaging/arch`. Put the value in `sha256sums` of `packaging/arch/PKGBUILD`, and commit it on `main` as
    `packaging(arch): checksum of the linux-vX.Y.Z source tarball`. This commit comes after the tag, as it did for
    2.2.0.
11. Test `makepkg` in an archlinux:latest container. Run it as a normal user, because makepkg refuses to run as
    root. `pyinstaller` is an AUR package (it is in `makedepends`), so the container needs it before `makepkg`
    runs.

## Security rules for contributors

- The helper is the boundary. Validate every value in `helper/protocol.py` before it is used. A new action goes
  into `ACTIONS` and `_PARAM_SPECS`, with a validator for each parameter. Do not validate in the caller and
  assume it holds.
- Values read from `state.json` are checked again on load (`state.py`) and before use. Keep those checks when you
  add a field.
- Ownership is checked by uid on every action that changes a binding, an adoption, an interface's DNS or limit,
  or a process. Use the existing checks (`engine._check_binding_owner`, `_check_iface_owner`, `binding_owner`).
  The uid comes from `PKEXEC_UID` or `SUDO_UID`, never from the request.
- Processes can be reused, so confirm a process is still the same one before you move or signal it: check its
  owner and its start time.
- Never write to the root cgroup. Release a process only to its own original cgroup, inside the owner's slice
  (`adopt.safe_release_target`, the `origins` of each adoption).
- Use `intermux/system/safefs.py` for any path under a user's home. Open such paths with no symlink following, as
  `engine._open_log` does (`O_NOFOLLOW`). Never follow a symlink that a user controls while running as root.
- No shell. Run commands only as argument lists through `system/commands.py`. Do not use `shell=True` or
  `os.system`.
- Environment variables for applications come only from `config.ENV_ALLOWLIST`, checked by `validate_env`.
- Interface names, namespace names, subnets, slots and the group path are checked by regular expression in both
  the protocol and the state loader. Keep both checks.
- A new action needs a protocol test in `tests/test_protocol.py` or `tests/test_input_hardening.py`, and an entry in
  `CHANGELOG.md`.
- Changes to the helper's checks matter more when passwordless mode is on. Any program running as the user can
  then send the helper a request. See the "Passwordless mode" section of `SECURITY.md`.
- Only the Python helper and watcher write `state.json`. A new writer must keep every field the Python model
  reads. Python drops keys it does not know when it saves.

## Gotchas learned

- `os.setns` exists only in Python 3.12 and newer. Before that, `system/netns.py` calls libc's `setns` through
  ctypes. The release bundle is built with Python 3.11, so the ctypes path is the one that runs in the bundle.
  Test namespace code on both paths.
- NetworkManager older than 1.42 does not have the `wireguard.*-auto-default-route` properties. Ubuntu 22.04
  ships 1.36. On those versions `nm.py` sets only `ipv4.never-default` and `ipv6.never-default`, so the VPN still
  does not become the default route.
- Ubuntu 22.04 has polkit 0.105. It reads `.pkla` files and does not read JavaScript rules. `passwordless.py`
  chooses the file type from the polkit version (below 0.106 means `.pkla`).
- systemd removes a scope when it becomes empty. A released process therefore needs a scope recreated for it, or a
  leaf group. `adopt.py` recreates one level. A parent that has controllers rejects processes with EBUSY.
- Routing a running app needs the nftables `socket cgroupv2` match. If the kernel was upgraded but not rebooted,
  the match may be missing, and `adopt` then fails with the error code `nft_unsupported` until the host is rebooted.
- The bundle needs glibc 2.35 or newer, so it must be built in ubuntu:22.04. See step 4 of the release steps.
- `dist/` and `build/` in the main checkout (`/home/rishi/Documents/Stuff/intermux`) are owned by root, so a normal
  user cannot write to them. Build in a worktree or a container instead of there.
- `install.sh` does not start the watcher (`intermux-watch.service`) on a system without systemd. There, idle
  bindings are removed only when `intermux teardown-idle` is run by hand.

## Current work

- The plan, decisions and the 3.0.0-beta.1 checklist: `docs/v3-plan.md` (Status section).
- The daemon contract, which all v3 code follows: `docs/daemon-protocol.md`.
- The state file reference: `docs/state-format.md`.
- Branches. At the time of writing, each of the four branches points at `c7f957f` (the protocol document), so check
  `git log` for progress.
  - `feat/v3-beta`: the 3.0.0-beta.1 integration branch: packaging, CI and the pre-release.
  - `feat/v3-daemon`: `intermuxd` and `rust/intermux-common`, as specified in `docs/daemon-protocol.md`.
  - `feat/v3-client`: the Python daemon client (`intermux/daemon_client.py`), `intermux events`, and the window as
    a daemon client.
  - `feat/v3-docs`: the v3 plan, the state format reference and this handoff guide.
- `intermux-tray` has no branch yet. The plan puts it in 3.0.0-beta.1.

## Where specs live

Task specs for in-flight work are in the scratchpad of the session that wrote them, under
`/tmp/claude-1000/-home-rishi-Documents-Stuff-intermux/937bfbc1-93b1-4898-a35d-2edd0ec8a324/scratchpad/specs/`.
The scratchpad is temporary and is not in the repository. Anything a future engineer needs must be in `docs/`: the
protocol, the state format, the plan and this file.
