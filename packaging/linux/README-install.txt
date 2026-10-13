InterMux for Linux - release tarball
====================================

Contents
  dist/intermux/                PyInstaller bundle (the program)
  packaging/linux/              polkit policy, desktop entry, icon, metainfo,
                                systemd watcher unit and preset
  install.sh, uninstall.sh      system-wide installer and remover
  dist/intermux.sha256          SHA-256 of the bundle tree (checked by install.sh)
  README.md, license

Install (as root, after unpacking the tarball as root so the files are
root-owned, e.g. "sudo tar -xzf ..."):

  sudo ./install.sh

install.sh refuses a bundle that is writable by a normal user unless you
pass --allow-local-bundle; the bundle runs as root through polkit.
Verify the tarball with the .sha256 file published next to it first.

Requirements
  iproute2, iptables, procps (sysctl), util-linux (runuser), polkit
  (pkexec), xdg-utils. A systemd host is needed for the routing watcher.

glibc floor
  The bundle is built on Ubuntu 22.04 and needs glibc 2.35 or newer
  (Ubuntu 22.04+, Debian 12+, Fedora 36+, Arch). It does NOT run on Debian 11,
  Ubuntu 20.04, RHEL/Rocky/Alma 8 or 9, or musl systems such as Alpine. On
  those, install from source: install.sh without a dist/ directory builds
  the bundle locally (needs python3, python3-venv and tkinter).
