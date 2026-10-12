# Starting point, untested. Dependency names and the PyInstaller build
# need to be checked against the target Fedora release.
%global debug_package %{nil}
%global __os_install_post %{nil}

Name:           intermux
Version:        2.0.0
Release:        1%{?dist}
Summary:        Bind individual applications to specific network interfaces
License:        MIT
URL:            https://github.com/Rishi-Bhati/intermux
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  python3-pyinstaller
BuildRequires:  python3-tkinter
BuildRequires:  systemd-rpm-macros
Requires:       iproute
Requires:       iptables
Requires:       polkit
Requires:       util-linux
Requires:       procps-ng

%description
InterMux runs a chosen application inside its own network namespace and
routes that namespace through a selected network interface.

Changing network configuration requires administrator authentication
through polkit.

%prep
%autosetup

%build
pyinstaller intermux_linux.spec --noconfirm \
    --distpath %{_builddir}/dist --workpath %{_builddir}/build

%install
install -d %{buildroot}/usr/lib/intermux
cp -a %{_builddir}/dist/intermux/. %{buildroot}/usr/lib/intermux/
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

%post
%systemd_post intermux-watch.service
/usr/bin/update-desktop-database -q %{_datadir}/applications &>/dev/null || :
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor &>/dev/null || :

%preun
%systemd_preun intermux-watch.service

%postun
%systemd_postun_with_restart intermux-watch.service
/usr/bin/update-desktop-database -q %{_datadir}/applications &>/dev/null || :
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor &>/dev/null || :

%files
%license license
/usr/lib/intermux/
%{_bindir}/intermux
%{_datadir}/polkit-1/actions/org.intermux.policy
%{_datadir}/applications/intermux.desktop
%{_datadir}/icons/hicolor/scalable/apps/intermux.svg
%{_datadir}/metainfo/org.intermux.InterMux.metainfo.xml
%{_unitdir}/intermux-watch.service

%changelog
* Tue Oct 13 2026 TODO Maintainer <maintainer@example.com> - 2.0.0-1
- Initial packaging (starting point, untested)
