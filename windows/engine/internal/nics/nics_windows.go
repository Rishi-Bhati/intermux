//go:build windows

package nics

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"sort"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.zx2c4.com/wireguard/windows/tunnel/winipcfg"
)

const (
	ipUnicastIF   = 31 // IPPROTO_IP (0) -> network byte order (htonl)
	ipv6UnicastIF = 31 // IPPROTO_IPV6 (41) -> host byte order
)

// SystemProvider enumerates Windows adapters using IP Helper APIs.
type SystemProvider struct {
	ExcludeAdapterName string
}

func sockaddrToNetip(sa *syscall.RawSockaddrAny) (netip.Addr, bool) {
	if sa == nil {
		return netip.Addr{}, false
	}
	switch sa.Addr.Family {
	case windows.AF_INET:
		v4 := (*syscall.RawSockaddrInet4)(unsafe.Pointer(sa))
		return netip.AddrFrom4(v4.Addr), true
	case windows.AF_INET6:
		v6 := (*syscall.RawSockaddrInet6)(unsafe.Pointer(sa))
		return netip.AddrFrom16(v6.Addr), true
	default:
		return netip.Addr{}, false
	}
}

func (p *SystemProvider) List(ctx context.Context, includeAll bool) ([]NICInfo, error) {
	bufLen := uint32(16384)
	var buf []byte
	for {
		buf = make([]byte, bufLen)
		err := windows.GetAdaptersAddresses(
			windows.AF_UNSPEC,
			windows.GAA_FLAG_INCLUDE_PREFIX|windows.GAA_FLAG_INCLUDE_GATEWAYS,
			0,
			(*windows.IpAdapterAddresses)(unsafe.Pointer(&buf[0])),
			&bufLen,
		)
		if err == nil {
			break
		}
		if err != windows.ERROR_BUFFER_OVERFLOW {
			return nil, err
		}
	}

	defaultLUIDs := make(map[winipcfg.LUID]uint32)
	if routes, err := winipcfg.GetIPForwardTable2(windows.AF_INET); err == nil {
		for _, r := range routes {
			if r.DestinationPrefix.PrefixLength == 0 {
				if prev, ok := defaultLUIDs[r.InterfaceLUID]; !ok || r.Metric < prev {
					defaultLUIDs[r.InterfaceLUID] = r.Metric
				}
			}
		}
	}

	var out []NICInfo
	aa := (*windows.IpAdapterAddresses)(unsafe.Pointer(&buf[0]))
	for ; aa != nil; aa = aa.Next {
		if aa.IfType == ifTypeSoftwareLoopback {
			continue
		}
		name := windows.UTF16PtrToString(aa.FriendlyName)
		if p.ExcludeAdapterName != "" && strings.EqualFold(name, p.ExcludeAdapterName) {
			continue
		}
		desc := windows.UTF16PtrToString(aa.Description)
		guid := windows.BytePtrToString(aa.AdapterName)
		kind := ClassifyKind(aa.IfType, name, desc)
		up := aa.OperStatus == windows.IfOperStatusUp

		var v4CIDRs, v6CIDRs, gateways, dnsServers []string
		for ua := aa.FirstUnicastAddress; ua != nil; ua = ua.Next {
			addr, ok := sockaddrToNetip(ua.Address.Sockaddr)
			if !ok {
				continue
			}
			if addr.Is4() {
				if addr.IsLinkLocalUnicast() {
					continue
				}
				v4CIDRs = append(v4CIDRs, fmt.Sprintf("%s/%d", addr, ua.OnLinkPrefixLength))
			} else if addr.Is6() {
				if addr.IsLinkLocalUnicast() {
					continue
				}
				v6CIDRs = append(v6CIDRs, fmt.Sprintf("%s/%d", addr, ua.OnLinkPrefixLength))
			}
		}
		for ga := aa.FirstGatewayAddress; ga != nil; ga = ga.Next {
			if addr, ok := sockaddrToNetip(ga.Address.Sockaddr); ok && !addr.IsUnspecified() {
				gateways = append(gateways, addr.String())
			}
		}
		for da := aa.FirstDnsServerAddress; da != nil; da = da.Next {
			if addr, ok := sockaddrToNetip(da.Address.Sockaddr); ok {
				if addr.IsLoopback() || strings.HasPrefix(addr.String(), "fec0:0:0:ffff::") {
					continue
				}
				dnsServers = append(dnsServers, addr.String())
			}
		}

		var mac string
		if aa.PhysicalAddressLength > 0 && int(aa.PhysicalAddressLength) <= len(aa.PhysicalAddress) {
			parts := make([]string, aa.PhysicalAddressLength)
			for i := uint32(0); i < aa.PhysicalAddressLength; i++ {
				parts[i] = fmt.Sprintf("%02x", aa.PhysicalAddress[i])
			}
			mac = strings.Join(parts, ":")
		}

		speedBps := uint64(0)
		if aa.TransmitLinkSpeed != ^uint64(0) && aa.TransmitLinkSpeed > speedBps {
			speedBps = aa.TransmitLinkSpeed
		}
		if aa.ReceiveLinkSpeed != ^uint64(0) && aa.ReceiveLinkSpeed > speedBps {
			speedBps = aa.ReceiveLinkSpeed
		}

		luid := winipcfg.LUID(aa.Luid)
		var rxBytes, txBytes uint64
		if row, err := luid.Interface(); err == nil && row != nil {
			rxBytes = row.InOctets
			txBytes = row.OutOctets
		}
		var metric uint32
		if ipif, err := luid.IPInterface(windows.AF_INET); err == nil && ipif != nil {
			metric = ipif.Metric
		}
		routeMetric, hasDefault := defaultLUIDs[luid]
		if hasDefault {
			metric += routeMetric
		}

		bindable := IsBindable(kind, up, v4CIDRs, len(gateways) > 0)
		if !includeAll && !bindable && !up {
			continue
		}

		out = append(out, NICInfo{
			Name:          name,
			Description:   desc,
			GUID:          guid,
			IfIndex:       aa.IfIndex,
			IfIndex6:      aa.Ipv6IfIndex,
			LUID:          aa.Luid,
			Kind:          kind,
			Up:            up,
			Bindable:      bindable,
			IPv4CIDRs:     v4CIDRs,
			IPv6CIDRs:     v6CIDRs,
			Gateways:      gateways,
			DNSServers:    dnsServers,
			MAC:           mac,
			MTU:           aa.Mtu,
			Metric:        metric,
			LinkSpeedBps:  float64(speedBps) / 8.0,
			LinkSpeedMbps: float64(speedBps) / 1e6,
			RxBytes:       rxBytes,
			TxBytes:       txBytes,
			DefaultRoute:  hasDefault,
		})
	}

	sort.SliceStable(out, func(i, j int) bool {
		if out[i].DefaultRoute != out[j].DefaultRoute {
			return out[i].DefaultRoute
		}
		if out[i].Up != out[j].Up {
			return out[i].Up
		}
		return out[i].Metric < out[j].Metric
	})
	return out, nil
}

func (p *SystemProvider) GetByName(ctx context.Context, name string) (NICInfo, error) {
	list, err := p.List(ctx, true)
	if err != nil {
		return NICInfo{}, err
	}
	for _, n := range list {
		if strings.EqualFold(n.Name, name) {
			return n, nil
		}
	}
	return NICInfo{}, fmt.Errorf("interface %q not found", name)
}

func (p *SystemProvider) DefaultPhysical(ctx context.Context) (NICInfo, error) {
	list, err := p.List(ctx, false)
	if err != nil {
		return NICInfo{}, err
	}
	for _, n := range list {
		if n.Up && n.DefaultRoute && n.Kind != KindVirtual && n.Kind != KindVPN && n.IfIndex != 0 {
			return n, nil
		}
	}
	for _, n := range list {
		if n.Up && n.DefaultRoute && n.Kind != KindVirtual && n.IfIndex != 0 {
			return n, nil
		}
	}
	for _, n := range list {
		if n.Bindable && n.IfIndex != 0 {
			return n, nil
		}
	}
	return NICInfo{}, errors.New("no active default physical interface available")
}

// SystemDialer pins outbound TCP/UDP sockets to a Windows adapter index via IP_UNICAST_IF / IPV6_UNICAST_IF.
type SystemDialer struct {
	Timeout time.Duration
}

func NewSystemDialer() *SystemDialer {
	return &SystemDialer{Timeout: 10 * time.Second}
}

func pinSocketV4(fd windows.Handle, ifIndex uint32) error {
	if ifIndex == 0 {
		return errors.New("invalid IPv4 interface index 0")
	}
	var b [4]byte
	binary.BigEndian.PutUint32(b[:], ifIndex)
	netOrder := *(*uint32)(unsafe.Pointer(&b[0]))
	return windows.SetsockoptInt(fd, windows.IPPROTO_IP, ipUnicastIF, int(netOrder))
}

func pinSocketV6(fd windows.Handle, ifIndex6 uint32) error {
	if ifIndex6 == 0 {
		return errors.New("invalid IPv6 interface index 0")
	}
	return windows.SetsockoptInt(fd, windows.IPPROTO_IPV6, ipv6UnicastIF, int(ifIndex6))
}

func controlPin(ifIndex uint32) func(network, address string, c syscall.RawConn) error {
	return func(network, address string, c syscall.RawConn) error {
		var sockErr error
		if err := c.Control(func(fd uintptr) {
			h := windows.Handle(fd)
			switch network {
			case "tcp4", "udp4", "ip4":
				sockErr = pinSocketV4(h, ifIndex)
			case "tcp6", "udp6", "ip6":
				sockErr = pinSocketV6(h, ifIndex)
			default:
				sockErr = pinSocketV4(h, ifIndex)
			}
		}); err != nil {
			return err
		}
		return sockErr
	}
}

func (d *SystemDialer) DialTCP(ctx context.Context, ifIndex uint32, network, addr string) (net.Conn, error) {
	if network == "tcp" {
		if host, _, err := net.SplitHostPort(addr); err == nil {
			if ip, perr := netip.ParseAddr(host); perr == nil {
				if ip.Is6() {
					network = "tcp6"
				} else {
					network = "tcp4"
				}
			} else {
				network = "tcp4"
			}
		}
	}
	dialer := &net.Dialer{
		Timeout: d.Timeout,
		Control: controlPin(ifIndex),
	}
	return dialer.DialContext(ctx, network, addr)
}

func (d *SystemDialer) ListenUDP(ctx context.Context, ifIndex uint32, network string) (net.PacketConn, error) {
	laddr := "0.0.0.0:0"
	if network == "udp6" {
		laddr = "[::]:0"
	} else {
		network = "udp4"
	}
	lc := &net.ListenConfig{
		Control: controlPin(ifIndex),
	}
	return lc.ListenPacket(ctx, network, laddr)
}
