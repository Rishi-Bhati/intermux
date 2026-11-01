# One-line installers

This directory holds the scripts behind InterMux's one-line install commands. They are served
straight from GitHub, so they work today without a website:

| Platform | Command |
|----------|---------|
| Linux (curl) | `curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh \| sh` |
| Linux (wget) | `wget -qO- https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh \| sh` |
| Windows (PowerShell) | `irm https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.ps1 \| iex` |

## `install.sh` (Linux)

POSIX `sh`, so it runs under dash, bash or busybox. Run it as your normal user; it uses `sudo` (or `doas`) when it needs root.

1. Finds the newest `linux-v*` release, or the one passed with `--version`.
2. On x86_64 with glibc 2.35 or newer it downloads `intermux-linux-x86_64.tar.gz`, checks it against the
   published `.sha256`, unpacks it as root and runs the release's own `install.sh`.
3. On anything else (aarch64, older glibc such as Ubuntu 20.04 or Debian 11, musl) it downloads the tagged
   source instead, and `install.sh` builds the bundle as your user.
4. Installs only the missing system packages (iproute2, iptables, polkit, ...) with pacman, apt, dnf/yum,
   zypper or apk. On Arch it never runs `pacman -Sy`, which would be a partial upgrade.
5. Refuses to touch an install that pacman, dpkg or rpm owns.

Options go after `sh -s --`:

```sh
curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh -s -- --version 2.0.1
curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh -s -- --from-source
curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh -s -- --no-deps
curl -fsSL https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.sh | sh -s -- --uninstall
```

Running it again updates to the newest release.

## `install.ps1` (Windows)

Downloads `InterMux.exe` from the newest release that has one (pre-releases included), checks it against
the SHA-256 digest GitHub publishes for release assets, and installs it per user to
`%LOCALAPPDATA%\Programs\InterMux` with a Start menu shortcut and an entry in Settings > Apps (which
uninstalls it). No admin rights are needed to install; InterMux asks for them when it starts.

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.ps1))) -Version 2.0
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get/install.ps1))) -Uninstall
```

## Serving the scripts from your own domain

The goal is `curl -fsSL https://intermux.dev/install | sh` (use whatever domain you own). The scripts stay
in this directory; the domain only redirects to them. `curl -L` (part of `-fsSL`), `wget` and `irm` all
follow redirects.

**From the Next.js site (Vercel or similar).** Add redirects in `next.config.ts`:

```ts
async redirects() {
  const raw = "https://raw.githubusercontent.com/Rishi-Bhati/intermux/main/get";
  return [
    { source: "/install", destination: `${raw}/install.sh`, permanent: false },
    { source: "/install.sh", destination: `${raw}/install.sh`, permanent: false },
    { source: "/install.ps1", destination: `${raw}/install.ps1`, permanent: false },
  ];
},
```

**With only DNS (Cloudflare).** Point a subdomain such as `get.intermux.dev` at Cloudflare and add
Redirect Rules for `/install` and `/install.ps1` to the raw URLs above (302).

Use temporary (302/307) redirects, not permanent ones, so you can move the target later. After the domain
works, update the commands in the main README, in the `RAW_URL` line of `install.sh` (it is printed in
the uninstall hint) and in the comments at the top of both scripts.

## Testing changes

```sh
sh -n get/install.sh && shellcheck -s sh get/install.sh
docker run --rm -v "$PWD/get:/g:ro" fedora:latest sh /g/install.sh
```

Containers have no systemd, so `install.sh` warns that the routing watcher was not started; that is expected.
