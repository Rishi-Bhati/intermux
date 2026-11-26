package dns

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"strings"
	"sync"
	"time"
)

const (
	MaxServers = 4
	Auto       = "auto"
	Keep       = "keep"
	ProbeQID   = 0x494D
)

var (
	PublicFallback = []string{"1.1.1.1", "9.9.9.9"}
	Presets        = map[string][]string{
		"cloudflare": {"1.1.1.1", "1.0.0.1"},
		"google":     {"8.8.8.8", "8.8.4.4"},
		"quad9":      {"9.9.9.9", "149.112.112.112"},
	}
)

// Dialer abstracts pinned outbound TCP/UDP creation for DNS forwarding and probing.
type Dialer interface {
	DialTCP(ctx context.Context, ifIndex uint32, network, addr string) (net.Conn, error)
	ListenUDP(ctx context.Context, ifIndex uint32, network string) (net.PacketConn, error)
}

// ValidateServer validates a single upstream IPv4 DNS server address (matching intermux/dnsconf.py).
func ValidateServer(raw string) (string, error) {
	addr, err := netip.ParseAddr(strings.TrimSpace(raw))
	if err != nil || !addr.Is4() {
		return "", fmt.Errorf("DNS server %q must be a valid IPv4 address", raw)
	}
	if addr.IsLoopback() || addr.IsLinkLocalUnicast() || addr.IsLinkLocalMulticast() ||
		addr.IsMulticast() || addr.IsUnspecified() || addr.String() == "255.255.255.255" {
		return "", fmt.Errorf("DNS server %q is not a valid unicast upstream address", raw)
	}
	// Reject 240.0.0.0/4 reserved space (except 255 already handled).
	b := addr.As4()
	if b[0] >= 240 {
		return "", fmt.Errorf("DNS server %q is in reserved IPv4 range", raw)
	}
	return addr.String(), nil
}

// ValidateServers validates 1..4 unique IPv4 DNS server strings (or resolves a preset name).
func ValidateServers(servers []string) ([]string, error) {
	if len(servers) == 0 || len(servers) > MaxServers {
		return nil, fmt.Errorf("provide 1 to %d DNS servers", MaxServers)
	}
	seen := make(map[string]struct{}, len(servers))
	out := make([]string, 0, len(servers))
	for _, s := range servers {
		norm, err := ValidateServer(s)
		if err != nil {
			return nil, err
		}
		if _, dup := seen[norm]; dup {
			continue
		}
		seen[norm] = struct{}{}
		out = append(out, norm)
	}
	return out, nil
}

// BuildProbeQuery builds the exact 17-byte root-NS DNS query used by InterMux Linux (QID=0x494D).
func BuildProbeQuery() []byte {
	return []byte{
		0x49, 0x4D, // ID = 0x494D ("IM")
		0x01, 0x00, // Flags: RD=1
		0x00, 0x01, // QDCOUNT = 1
		0x00, 0x00, // ANCOUNT = 0
		0x00, 0x00, // NSCOUNT = 0
		0x00, 0x00, // ARCOUNT = 0
		0x00,       // QNAME = "."
		0x00, 0x02, // QTYPE = NS (2)
		0x00, 0x01, // QCLASS = IN (1)
	}
}

// IsValidProbeReply returns true if reply is a valid DNS response for BuildProbeQuery (NOERROR or NXDOMAIN).
func IsValidProbeReply(reply []byte) bool {
	if len(reply) < 12 {
		return false
	}
	qid := binary.BigEndian.Uint16(reply[0:2])
	flags := binary.BigEndian.Uint16(reply[2:4])
	if qid != ProbeQID {
		return false
	}
	if flags&0x8000 == 0 {
		return false
	}
	rcode := flags & 0x000F
	return rcode == 0 || rcode == 3
}

// OrderServers returns answering servers first (preserving order), followed by non-answering servers.
func OrderServers(candidates, answering []string) []string {
	ansSet := make(map[string]struct{}, len(answering))
	for _, a := range answering {
		ansSet[a] = struct{}{}
	}
	first := make([]string, 0, len(candidates))
	rest := make([]string, 0, len(candidates))
	for _, c := range candidates {
		if _, ok := ansSet[c]; ok {
			first = append(first, c)
		} else {
			rest = append(rest, c)
		}
	}
	return append(first, rest...)
}

// BindingConfig stores resolved upstream servers for an interface.
type BindingConfig struct {
	Servers   []string
	Automatic bool
}

// Manager manages per-interface DNS policies, probing, and port-53 forwarding.
type Manager struct {
	mu      sync.RWMutex
	dialer  Dialer
	configs map[string]BindingConfig
}

// NewManager initializes a DNS Manager.
func NewManager(dialer Dialer) *Manager {
	return &Manager{
		dialer:  dialer,
		configs: make(map[string]BindingConfig),
	}
}

// SetPolicy stores DNS configuration for iface.
func (m *Manager) SetPolicy(iface string, servers []string, automatic bool) {
	cp := append([]string(nil), servers...)
	m.mu.Lock()
	m.configs[iface] = BindingConfig{Servers: cp, Automatic: automatic}
	m.mu.Unlock()
	FlushSystemCache()
}

// ClearPolicy removes per-interface DNS configuration.
func (m *Manager) ClearPolicy(iface string) {
	m.mu.Lock()
	delete(m.configs, iface)
	m.mu.Unlock()
}

// ServersFor returns configured servers for iface, falling back to fallbackServers or PublicFallback.
func (m *Manager) ServersFor(iface string, fallbackServers []string) []string {
	m.mu.RLock()
	cfg, ok := m.configs[iface]
	m.mu.RUnlock()
	if ok && len(cfg.Servers) > 0 {
		return append([]string(nil), cfg.Servers...)
	}
	if len(fallbackServers) > 0 {
		return append([]string(nil), fallbackServers...)
	}
	return append([]string(nil), PublicFallback...)
}

// Snapshot returns current DNS server lists per interface.
func (m *Manager) Snapshot() map[string][]string {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make(map[string][]string, len(m.configs))
	for k, v := range m.configs {
		out[k] = append([]string(nil), v.Servers...)
	}
	return out
}

// ProbeServers sends root-NS UDP queries pinned to ifIndex and returns answering servers.
func (m *Manager) ProbeServers(ctx context.Context, ifIndex uint32, servers []string, timeout time.Duration) []string {
	if m.dialer == nil || len(servers) == 0 {
		return nil
	}
	if timeout <= 0 {
		timeout = 1500 * time.Millisecond
	}
	query := BuildProbeQuery()
	var mu sync.Mutex
	ansSet := make(map[string]bool, len(servers))
	var wg sync.WaitGroup

	for _, srv := range servers {
		srv := srv
		wg.Add(1)
		go func() {
			defer wg.Done()
			pc, err := m.dialer.ListenUDP(ctx, ifIndex, "udp4")
			if err != nil {
				return
			}
			defer pc.Close()
			_ = pc.SetDeadline(time.Now().Add(timeout))
			dst, err := net.ResolveUDPAddr("udp4", net.JoinHostPort(srv, "53"))
			if err != nil {
				return
			}
			if _, err := pc.WriteTo(query, dst); err != nil {
				return
			}
			buf := make([]byte, 512)
			n, _, err := pc.ReadFrom(buf)
			if err != nil {
				return
			}
			if IsValidProbeReply(buf[:n]) {
				mu.Lock()
				ansSet[srv] = true
				mu.Unlock()
			}
		}()
	}
	wg.Wait()

	answering := make([]string, 0, len(ansSet))
	for _, s := range servers {
		if ansSet[s] {
			answering = append(answering, s)
		}
	}
	return answering
}

// ForwardUDP forwards a raw port-53 DNS datagram to the first responsive upstream server over ifIndex.
func (m *Manager) ForwardUDP(ctx context.Context, payload []byte, ifIndex uint32, upstreams []string) ([]byte, error) {
	if m.dialer == nil {
		return nil, errors.New("dns dialer unavailable")
	}
	if len(payload) < 2 {
		return nil, errors.New("truncated DNS query")
	}
	if len(upstreams) == 0 {
		upstreams = PublicFallback
	}
	txID := binary.BigEndian.Uint16(payload[:2])
	var lastErr error
	for _, srv := range upstreams {
		pc, err := m.dialer.ListenUDP(ctx, ifIndex, "udp4")
		if err != nil {
			lastErr = err
			continue
		}
		_ = pc.SetDeadline(time.Now().Add(2500 * time.Millisecond))
		dst, err := net.ResolveUDPAddr("udp4", net.JoinHostPort(srv, "53"))
		if err != nil {
			_ = pc.Close()
			lastErr = err
			continue
		}
		if _, err := pc.WriteTo(payload, dst); err != nil {
			_ = pc.Close()
			lastErr = err
			continue
		}
		buf := make([]byte, 4096)
		n, _, err := pc.ReadFrom(buf)
		_ = pc.Close()
		if err != nil {
			lastErr = err
			continue
		}
		if n < 12 || binary.BigEndian.Uint16(buf[:2]) != txID {
			lastErr = errors.New("DNS response transaction ID mismatch")
			continue
		}
		return append([]byte(nil), buf[:n]...), nil
	}
	if lastErr == nil {
		lastErr = errors.New("all upstream DNS servers failed")
	}
	return nil, lastErr
}

// ForwardTCP relays framed RFC 1035 TCP port-53 messages between clientConn and an upstream DNS server.
func (m *Manager) ForwardTCP(ctx context.Context, clientConn net.Conn, ifIndex uint32, upstreams []string) error {
	if m.dialer == nil {
		return errors.New("dns dialer unavailable")
	}
	if len(upstreams) == 0 {
		upstreams = PublicFallback
	}
	var upConn net.Conn
	var lastErr error
	for _, srv := range upstreams {
		c, err := m.dialer.DialTCP(ctx, ifIndex, "tcp4", net.JoinHostPort(srv, "53"))
		if err != nil {
			lastErr = err
			continue
		}
		upConn = c
		break
	}
	if upConn == nil {
		if lastErr == nil {
			lastErr = errors.New("no upstream DNS server reachable over TCP")
		}
		return lastErr
	}
	defer upConn.Close()

	errCh := make(chan error, 2)
	go func() {
		_, err := io.Copy(upConn, clientConn)
		errCh <- err
	}()
	go func() {
		_, err := io.Copy(clientConn, upConn)
		errCh <- err
	}()
	select {
	case err := <-errCh:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}
