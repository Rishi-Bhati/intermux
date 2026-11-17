#!/bin/bash
# Daemon, user units and tray end to end, inside a container running systemd as PID 1.
#   docker build -t im-systemd -f tests/e2e/Dockerfile.systemd tests/e2e
#   docker run -d --name im-v3 --privileged --tmpfs /run --tmpfs /run/lock -v "$OUT:/out:ro" -v "$PWD/tests/e2e:/e2e:ro" im-systemd
#   (wait until `docker exec im-v3 systemctl is-system-running` says running or degraded)
#   docker exec -e EXPECT_VERSION=3.0.0-beta.1 im-v3 bash /e2e/daemon-e2e.sh; docker rm -f im-v3
set -u
pass(){ echo "OK   $*"; }; fail(){ echo "FAIL $*"; FAILED=1; }; FAILED=0
cd /tmp && tar xzf /out/intermux-linux-x86_64.tar.gz && cd intermux-linux
./install.sh --no-passwordless </dev/null >/tmp/install.log 2>&1 && pass "install" || { fail "install"; tail -20 /tmp/install.log; exit 1; }
V=$(runuser -u nobody -- /usr/lib/intermux/intermux --version 2>&1); [ -n "${EXPECT_VERSION:-}" ] && { echo "$V" | grep -qF "$EXPECT_VERSION" && pass "version $V" || fail "version $V"; } || pass "version $V"
for f in /usr/lib/intermux/intermuxd /usr/lib/intermux/intermux-tray; do [ -x "$f" ] && pass "$f installed" || fail "$f missing"; done
[ -f /usr/lib/systemd/user/intermuxd.socket ] && [ -f /usr/lib/systemd/user/intermuxd.service ] && pass "user units installed" || fail "user units"
[ -e /etc/systemd/user/sockets.target.wants/intermuxd.socket ] && pass "socket enabled globally" || fail "socket not enabled globally"
[ -f /usr/share/applications/intermux-tray.desktop ] && pass "tray desktop entry" || fail "tray desktop entry"
useradd -m tester; U=$(id -u tester)
loginctl enable-linger tester
for i in $(seq 1 30); do systemctl is-active -q user@$U.service && break; sleep 1; done
systemctl is-active -q user@$U.service && pass "user manager running" || fail "user manager"
asu(){ runuser -u tester -- env XDG_RUNTIME_DIR=/run/user/$U DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$U/bus "$@"; }
for i in $(seq 1 10); do asu systemctl --user is-active -q intermuxd.socket && break; sleep 1; done
asu systemctl --user is-active -q intermuxd.socket && pass "intermuxd.socket active for the user" || fail "socket not active: $(asu systemctl --user status intermuxd.socket 2>&1 | tail -3)"
[ -S /run/user/$U/intermux/daemon.sock ] && pass "socket file $(stat -c '%a %U' /run/user/$U/intermux/daemon.sock), dir $(stat -c '%a' /run/user/$U/intermux)" || fail "no socket file"
asu timeout 12 intermux events --json --topics interfaces,bindings > /tmp/events.json 2>/tmp/events.err &
sleep 2
asu systemctl --user is-active -q intermuxd.service && pass "daemon socket-activated" || fail "daemon not activated: $(asu systemctl --user status intermuxd.service 2>&1 | tail -5)"
printf '#!/bin/sh\nexec sleep 600\n' > /usr/local/bin/stay; chmod 755 /usr/local/bin/stay
UP=$(ip -j route show default | sed -n 's/.*"dev":"\([^"]*\)".*/\1/p' | head -1)
printf '{"v":1,"action":"bind","params":{"iface":"%s","app":"/usr/local/bin/stay","dns":"auto"}}' "$UP" | PKEXEC_UID=$U /usr/lib/intermux/intermux helper > /tmp/bind.json 2>/tmp/bind.err
grep -q '"ok": true' /tmp/bind.json && pass "bind via helper" || { fail "bind"; cat /tmp/bind.json; tail -3 /tmp/bind.err; }
sleep 4
grep -q '"binding.started"' /tmp/events.json && pass "event binding.started received" || { fail "no binding.started"; cat /tmp/events.json | head -5; cat /tmp/events.err | tail -3; }
grep -q '"interface.status"' /tmp/events.json && pass "event interface.status received (routed count changed)" || echo "INFO no interface.status event"
pkill -u tester -f 'sleep 600'; sleep 4
wait
grep -q '"binding.stopped"\|"app.exited"' /tmp/events.json && pass "event after the app exited: $(grep -o '"event": *"[a-z.]*"' /tmp/events.json | sort | uniq -c | tr '\n' ' ')" || fail "no stop/exit event: $(grep -o '"event": *"[a-z.]*"' /tmp/events.json | sort | uniq -c | tr '\n' ' ')"
asu /usr/lib/intermux/intermux-tray --version && pass "tray --version"
asu timeout 5 /usr/lib/intermux/intermux-tray > /tmp/tray.log 2>&1; rc=$?
[ $rc -eq 124 ] && pass "tray keeps running without a tray area (no crash)" || { fail "tray exited rc=$rc"; tail -5 /tmp/tray.log; }
asu timeout 5 /usr/lib/intermux/intermux-tray >/dev/null 2>&1 & sleep 1; asu timeout 3 /usr/lib/intermux/intermux-tray; [ $? -eq 0 ] && pass "second tray exits 0 (single instance)" || fail "second tray"
wait
./uninstall.sh --force </dev/null >/tmp/uninstall.log 2>&1 && pass "uninstall" || fail "uninstall"
[ ! -e /usr/lib/systemd/user/intermuxd.socket ] && [ ! -e /etc/systemd/user/sockets.target.wants/intermuxd.socket ] && pass "units removed and disabled" || fail "units left behind"
[ ! -e /usr/share/applications/intermux-tray.desktop ] && pass "tray desktop entry removed" || fail "tray desktop entry left"
[ $FAILED -eq 0 ] && echo "ALL OK" || echo "SOME FAILED"
