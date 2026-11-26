# InterMux Windows Networking Architecture (`net-merge` Analysis & Design)

## 1. Research Summary: How `net-merge` Manages Networking on Windows

Analysis of `github.com/net-merge/net-merge` (MIT, Go 1.27) revealed a clean, production-tested userland packet-routing architecture on Windows that avoids custom kernel drivers and Winsock DLL injection:

1. **Wintun L3 Virtual Adapter (`wintun.dll`, signed by WireGuard LLC)**
   - Creates a virtual L3 TUN adapter (default MTU `9000`) in userland via a Microsoft-signed kernel TUN driver (`wintun.dll`), avoiding EV/WHCP kernel-driver signing.
   - Dynamically selects a non-colliding TUN subnet (avoiding `198.18.0.0/24` collisions with Clash, sing-box, v2rayN, etc.).
2. **Failsafe `/1` Route Injection**
   - Installs split default routes (`0.0.0.0/1` + `128.0.0.0/1`, and `::/1` + `8000::/1` for IPv6) rather than deleting the host's `/0` default gateway.
   - Because `/0` stays intact in the OS routing table, tearing down the Wintun adapter on exit or crash immediately restores host connectivity.
3. **Userland TCP/UDP Termination (`tun2socks/v2` + `gVisor`)**
   - Terminates raw IPv4/IPv6 packets from Wintun in user space via `gVisor`'s battle-tested TCP/UDP stack, avoiding fragile custom userland TCP state machines and supporting both TCP and UDP transparently (regardless of whether an application honors `HTTP_PROXY`/`ALL_PROXY` environment variables).
4. **Windows Egress Socket Pinning (`IP_UNICAST_IF` / `IPV6_UNICAST_IF`)**
   - Outbound sockets re-dialed from the userland stack are pinned directly to the chosen physical or VPN adapter's interface index (`IPPROTO_IP`/`IP_UNICAST_IF = 31` in network byte order for IPv4; `IPPROTO_IPV6`/`IPV6_UNICAST_IF = 31` in host byte order for IPv6).
   - Egress pinning bypasses the `/1` Wintun routes cleanly without creating routing loops, directly paralleling Linux `SO_BINDTOIFINDEX`, macOS `IP_BOUND_IF`, and Android `VpnService.protect()`.
5. **Userland Shaping, Telemetry & UAC-Free Autostart**
   - Uses `golang.org/x/time/rate` token-bucket rate limiters, OS interface byte counters (`GetIfEntry2`) + userland relayed counters, an unprivileged SOCKS5 front-end, and a Windows Scheduled Task for elevated autostart without repeated UAC prompts.

## 2. Why `net-merge` Alone Is Insufficient — And Why Alternatives Were Rejected

`net-merge` is a **system-wide multi-WAN connection load balancer** (`(busy_flows + 1) / weight`), not a per-application interface binder. InterMux requires binding individual apps (and already-running PID trees) to specific interfaces with per-binding speeds, limits, and DNS.

Alternatives evaluated and rejected as primary transport:
- **Winsock DLL Injection (`ForceBindIP` style)**: Fails across 32/64-bit boundaries, UWP/AppContainer, Protected Processes (PPL), kernel anti-cheat, Go/WinRT direct syscalls, and Windows `svchost.exe` DNS.
- **Custom WFP Callout Driver**: Requires EV/WHCP/Microsoft Attestation kernel-driver signing and C/WDK kernel maintenance.
- **WinDivert**: Pre-signed, but LGPL-3.0 (incompatible with MIT), requires maintaining a custom stateful userland NAT/TCP/UDP engine, and is frequently blocked by anti-cheat software.
- **Phase-1 Python TCP SOCKS5 (`windows/core/proxy_engine.py`)**: Ignores UDP, bypassed by apps ignoring `ALL_PROXY`, lacks live speeds, bandwidth limits, DNS control, and running-app adoption.

## 3. InterMux Windows v2.2.1 Architecture

InterMux combines `net-merge`'s `Wintun + tun2socks/gVisor + IP_UNICAST_IF` transport with **per-application and per-PID-tree routing** in a dedicated Go networking engine (`windows/engine/`, compiled as `intermux-net.exe`) paired with a clean Python platform abstraction (`intermux/platform/`):

- **PID & Process-Tree Attribution**: Arriving L4 flows in gVisor are matched to OS PIDs via Windows IP Helper (`GetExtendedTcpTable` / `GetExtendedUdpTable` with `0.0.0.0`/`::` wildcard fallback for unconnected UDP) + `OpenProcess`/`QueryFullProcessImageNameW` + same-SID process-tree inheritance tracking (`CreateToolhelp32Snapshot`).
  - Bound/adopted PIDs route to their assigned adapter via `IP_UNICAST_IF`/`IPV6_UNICAST_IF` with **fail-closed kill-switch semantics** (if the bound adapter goes down, flows are dropped rather than leaking onto the default NIC).
  - Unbound PIDs route transparently out the host's real physical default adapter via `IP_UNICAST_IF`.
- **Live Speed Reporting**: Atomic per-interface, per-binding, and per-PID `rx_bytes`/`tx_bytes` counters in the Go data plane + 1 Hz rate sampling, matching Linux `RateTracker` and GUI 60-second dual-line sparklines.
- **Bidirectional Speed Limits**: Token-bucket limiters (`golang.org/x/time/rate`, `0.064..100000` Mbit/s) for `down` and `up` per bound interface, dynamically reconfigurable via IPC without dropping active connections and preserving unsplit UDP datagrams.
- **DNS Steering & `svchost.exe` (`dnscache`) Caveat**: Port-53 UDP/TCP flows are intercepted in the gVisor stack and forwarded over `IP_UNICAST_IF`-pinned upstream sockets to per-binding configured servers (Presets, Custom 1–4 IPv4, or `Automatic` adapter DNS, with `0x494D` root-NS probing).
  - *Windows `dnscache` caveat*: Applications using `Winsock getaddrinfo()` delegate DNS RPC calls to `svchost.exe` (`Dnscache`), which caches globally and uses Smart Multi-Homed Name Resolution. InterMux flushes the resolver cache (`DnsFlushResolverCache`) on DNS/bind changes, routes `svchost.exe` cleanly via pinned sockets, and documents this behavior explicitly in `set_dns` warnings, while direct-DNS apps and DoH-enabled browsers receive strict per-binding DNS isolation.
- **Unprivileged SOCKS5 Fallback**: Built-in `127.0.0.1` TCP `CONNECT` + `UDP ASSOCIATE` proxy (requiring per-session random RFC 1929 username/password auth) sharing the exact same counters, shaping, and pinned DNS resolvers when Administrator/Wintun is unavailable.
- **Clean Cross-Platform Seam (`intermux/platform/`)**: Preserves all existing Linux `intermux/system/*` modules and tests untouched while enabling Windows parity and future macOS (`utun` + `IP_BOUND_IF`) and Android (`VpnService` + `protect()`) ports.
