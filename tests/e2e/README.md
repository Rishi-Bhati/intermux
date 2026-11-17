# Docker end-to-end tests

Run on a Linux host with Docker (no sudo needed). They never touch the host's networking.

| Script | What it checks | Run |
|---|---|---|
| `build-bundle.sh` | Builds the PyInstaller bundle and release tarball inside `ubuntu:22.04` (glibc 2.35, the release target). Expects `/src` = clean `git archive` of the commit, `/out` = output dir. | `docker run --rm -v "$SRC:/src:ro" -v "$OUT:/out" -v "$PWD/tests/e2e:/b:ro" ubuntu:22.04 sh /b/build-bundle.sh` |
| `installtest.sh` | Installs the tarball, runs CLI checks as a normal user, binds an app through the helper (DNS probe inside the namespace), uninstalls. Needs `--privileged`. | `docker run --rm --privileged -v "$OUT:/out:ro" -v "$PWD/tests/e2e:/b:ro" ubuntu:22.04 sh /b/installtest.sh` (also `debian:12`) |
| `run-all.sh` / `polkit-e2e.sh` | Passwordless mode end to end with a real polkitd on Ubuntu 22.04 (pkla), Debian 12, Fedora 41 and Arch (JS rules): install with `--passwordless`, rule and group, members only, no grant outside an active session, group change without re-login, refused under pkexec, `off --all` and uninstall clean up. | `bash tests/e2e/run-all.sh tarball "$OUT"` (or `manual` to test the rule files alone) |

Note: `installtest.sh` binds through the real kernel of the host (namespaces, nftables). On a host whose kernel
was upgraded but not rebooted, modules such as `nft_socket` may be missing; reboot rather than loading modules
by hand.
