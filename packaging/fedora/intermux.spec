# Starting point, untested. Dependency names and the PyInstaller build
# need to be checked against the target Fedora release.
%global debug_package %{nil}
%global __os_install_post %{nil}
# The bundle carries its own private libpython, libssl, libtk, ...: do not
# publish them as Provides or derive Requires from them.
%global __provides_exclude_from ^%{_prefix}/lib/intermux/.*$
%global __requires_exclude_from ^%{_prefix}/lib/intermux/.*$

# NOTE: python3-pyinstaller is NOT in the official Fedora/EPEL repositories;
# this spec is only buildable from a COPR or private repo that provides it
# (PyInstaller 6.20.0 is the verified version). Official Fedora also forbids
# bundled-runtime packages.

Name:           intermux
Version:        2.0.0
Release:        1%{?dist}
Summary:        Bind individual applications to specific network interfaces
License:        MIT
URL:            https://github.com/Rishi-Bhati/intermux
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/%{name}-%{version}.tar.gz

BuildRequires:  python3-pyinstaller
BuildRequires:  python3-tkinter
BuildRequires:  systemd-rpm-macros
BuildRequires:  binutils
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
Requires:       iproute
Requires:       iptables-nft
Requires:       polkit
Requires:       util-linux
Requires:       procps-ng
Requires:       xdg-utils
Requires:       hicolor-icon-theme
Recommends:     nftables
Recommends:     curl
Provides:       bundled(python3)

%description
InterMux runs a chosen application inside its own network namespace and
routes that namespace through a selected network interface.

Changing network configuration requires administrator authentication
through polkit.

%prep
%autosetup

%build
# Keep PyInstaller's cache in the build tree (mock has no usable $HOME)
export PYINSTALLER_CONFIG_DIR="$PWD/pyi-config"
pyinstaller intermux_linux.spec --noconfirm \
    --distpath dist --workpath build

%install
install -d %{buildroot}/usr/lib/intermux
cp -a dist/intermux/. %{buildroot}/usr/lib/intermux/
chmod -R a+rX %{buildroot}/usr/lib/intermux
chmod 755 %{buildroot}/usr/lib/intermux/intermux

install -d %{buildroot}%{_bindir}
ln -s /usr/lib/intermux/intermux %{buildroot}%{_bindir}/intermux

install -Dm644 packaging/linux/org.intermux.policy \
    %{buildroot}%{_datadir}/polkit-1/actions/org.intermux.policy
install -Dm644 packaging/linux/intermux.desktop \
    %{buildroot}%{_datadir}/applications/intermux.desktop
install -Dm644 packaging/linux/intermux.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/intermux.svg
install -Dm644 packaging/linux/intermux.metainfo.xml \
    %{buildroot}%{_datadir}/metainfo/org.intermux.InterMux.metainfo.xml
install -Dm644 packaging/linux/intermux-watch.service \
    %{buildroot}%{_unitdir}/intermux-watch.service
# Preset so %systemd_post enables the watcher on install
install -Dm644 packaging/linux/intermux-watch.preset \
    %{buildroot}%{_presetdir}/90-intermux.preset

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/intermux.desktop
appstream-util validate-relax --nonet \
    %{buildroot}%{_datadir}/metainfo/org.intermux.InterMux.metainfo.xml

%post
%systemd_post intermux-watch.service
/usr/bin/update-desktop-database -q %{_datadir}/applications >/dev/null 2>&1 || :
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor >/dev/null 2>&1 || :

%preun
%systemd_preun intermux-watch.service

%postun
%systemd_postun_with_restart intermux-watch.service
/usr/bin/update-desktop-database -q %{_datadir}/applications >/dev/null 2>&1 || :
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor >/dev/null 2>&1 || :

%files
%license license
/usr/lib/intermux/
%{_bindir}/intermux
%{_datadir}/polkit-1/actions/org.intermux.policy
%{_datadir}/applications/intermux.desktop
%{_datadir}/icons/hicolor/scalable/apps/intermux.svg
%{_datadir}/metainfo/org.intermux.InterMux.metainfo.xml
%{_unitdir}/intermux-watch.service
%{_presetdir}/90-intermux.preset

%changelog
* Tue Oct 13 2026 TODO Maintainer <maintainer@example.com> - 2.0.0-1
- Initial packaging (starting point, untested)
