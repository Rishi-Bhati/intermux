#!/bin/sh
# Runs inside ubuntu:22.04 or debian:12 (privileged). /out holds the tarball. Installs, runs CLI checks as a
# normal user, checks the setns fallback in the frozen bundle, then uninstalls.
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq iproute2 iptables procps policykit-1 sudo >/dev/null 2>&1 || \
  apt-get install -y -qq iproute2 iptables procps polkitd pkexec sudo >/dev/null
. /etc/os-release; echo "== $PRETTY_NAME, glibc $(ldd --version | head -1 | awk '{print $NF}')"
mkdir -p /t && cd /t && tar xzf /out/intermux-linux-x86_64.tar.gz && cd intermux-linux
./install.sh </dev/null >/t/install.log 2>&1 || { tail -30 /t/install.log; exit 1; }
echo "install: ok"
useradd -m tester
su tester -c 'intermux --version; intermux interfaces >/dev/null && echo "interfaces: ok"; intermux status >/dev/null && echo "status: ok"; intermux check | tail -3'
# setns fallback inside the frozen bundle: a bind runs the DNS probe in the new namespace. Before the fix
# it always warned "no DNS server answered"; the container has internet, so a public resolver must answer.
printf "#!/bin/sh\nexec sleep 600\n" > /usr/local/bin/stay && chmod 755 /usr/local/bin/stay
UPLINK=$(ip -j route show default | sed -n 's/.*"dev":"\([^"]*\)".*/\1/p' | head -1)
printf '{"v":1,"action":"bind","params":{"iface":"%s","app":"/usr/local/bin/stay","dns":"auto"}}' "$UPLINK" \
  | PKEXEC_UID=$(id -u tester) /usr/lib/intermux/intermux helper 2>&1 | tee /t/bind.log | cut -c1-400
grep -q '"ok": *true' /t/bind.log || { echo "bind: FAILED"; exit 1; }
if grep -qE "DNS probe in .* failed|no DNS server answered" /t/bind.log; then echo "DNS probe: FAILED"; exit 1; else echo "DNS probe: ok"; fi
su tester -c "intermux status" | head -6
printf '{"v":1,"action":"reset","params":{"kill_apps":true}}' | PKEXEC_UID=$(id -u tester) /usr/lib/intermux/intermux helper >/dev/null 2>&1 || true
./uninstall.sh </dev/null >/t/uninstall.log 2>&1 || { tail -30 /t/uninstall.log; exit 1; }
test ! -e /usr/lib/intermux && echo "uninstall: ok (files removed)"
