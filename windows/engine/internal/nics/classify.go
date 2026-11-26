package nics

import (
	"context"
	"net"
	"strings"
)

// Kind represents a normalized adapter category matching Linux netinfo.py.
type Kind string

const (
	KindWifi      Kind = "wifi"
	KindEthernet  Kind = "ethernet"
	KindUSB       Kind = "usb"
	KindBluetooth Kind = "bluetooth"
	KindMobile    Kind = "mobile"
	KindVPN       Kind = "vpn"
	KindVirtual   Kind = "virtual"
	KindOther     Kind = "other"
)

// NICInfo describes a Windows network adapter in a cross-platform schema.
type NICInfo struct {
	Name          string   `json:"name"`
	Description   string   `json:"description"`
	GUID          string   `json:"guid"`
	IfIndex       uint32   `json:"index"`
	IfIndex6      uint32   `json:"if_index_v6"`
	LUID          uint64   `json:"luid"`
	Kind          Kind     `json:"kind"`
	Up            bool     `json:"up"`
	Bindable      bool     `json:"bindable"`
	IPv4CIDRs     []string `json:"ipv4_cidrs"`
	IPv6CIDRs     []string `json:"ipv6_cidrs"`
	Gateways      []string `json:"gateways"`
	DNSServers    []string `json:"dns_servers"`
	MAC           string   `json:"mac"`
	MTU           uint32   `json:"mtu"`
	Metric        uint32   `json:"metric"`
	LinkSpeedBps  float64  `json:"link_speed_bps"`
	LinkSpeedMbps float64  `json:"link_speed_mbps"`
	RxBytes       uint64   `json:"rx_bytes"`
	TxBytes       uint64   `json:"tx_bytes"`
	DefaultRoute  bool     `json:"default_route"`
}

// Provider enumerates network adapters.
type Provider interface {
	List(ctx context.Context, includeAll bool) ([]NICInfo, error)
	GetByName(ctx context.Context, name string) (NICInfo, error)
	DefaultPhysical(ctx context.Context) (NICInfo, error)
}

// OutboundDialer creates outbound sockets pinned to a specific interface index.
type OutboundDialer interface {
	DialTCP(ctx context.Context, ifIndex uint32, network, addr string) (net.Conn, error)
	ListenUDP(ctx context.Context, ifIndex uint32, network string) (net.PacketConn, error)
}

const (
	ifTypeEthernetCSMACD  = 6
	ifTypePPP             = 23
	ifTypePropVirtual     = 53
	ifTypeIEEE80211       = 71
	ifTypeTunnel          = 131
	ifTypeWWANPP          = 243
	ifTypeWWANPP2         = 244
	ifTypeSoftwareLoopback = 24
)

var vpnKeywords = []string{
	"wireguard", "wintun", "openvpn", "tap-windows", "tap0901", "nordlynx",
	"mullvad", "proton", "tailscale", "zerotier", "cisco", "anyconnect",
	"fortinet", "globalprotect", "pulse", "ras", "wan miniport", "ikev2",
	"l2tp", "pptp", "sstp", "vpn", "cloudflare warp",
}

var virtualKeywords = []string{
	"hyper-v", "vethernet", "vmware", "virtualbox", "vbox", "wsl", "docker",
	"npcap", "loopback", "teredo", "isatap", "6to4", "kernel debug",
}

var usbKeywords = []string{"rndis", "usb", "tether", "apple mobile", "android"}
var btKeywords = []string{"bluetooth", "bnep", "pan"}
var mobileKeywords = []string{"cellular", "mobile broadband", "wwan", "4g lte", "5g nr"}
var wifiKeywords = []string{"wi-fi", "wifi", "wlan", "802.11", "wireless"}

func containsAny(s string, words []string) bool {
	for _, w := range words {
		if strings.Contains(s, w) {
			return true
		}
	}
	return false
}

// ClassifyKind determines adapter kind from Windows IfType, description, and friendly name.
func ClassifyKind(ifType uint32, name, desc string) Kind {
	combined := strings.ToLower(name + " " + desc)
	if containsAny(combined, vpnKeywords) || ifType == ifTypeTunnel || ifType == ifTypePPP {
		return KindVPN
	}
	if containsAny(combined, virtualKeywords) || ifType == ifTypeSoftwareLoopback {
		return KindVirtual
	}
	if ifType == ifTypeIEEE80211 || containsAny(combined, wifiKeywords) {
		return KindWifi
	}
	if ifType == ifTypeWWANPP || ifType == ifTypeWWANPP2 || containsAny(combined, mobileKeywords) {
		return KindMobile
	}
	if containsAny(combined, usbKeywords) {
		return KindUSB
	}
	if containsAny(combined, btKeywords) {
		return KindBluetooth
	}
	if ifType == ifTypeEthernetCSMACD {
		return KindEthernet
	}
	if ifType == ifTypePropVirtual {
		return KindVirtual
	}
	return KindOther
}

// IsBindable determines if an adapter can be bound to by InterMux (including VPN tunnels).
func IsBindable(kind Kind, up bool, ipv4CIDRs []string, hasGateway bool) bool {
	if !up || len(ipv4CIDRs) == 0 || kind == KindVirtual {
		return false
	}
	if kind == KindVPN {
		return true
	}
	return hasGateway || len(ipv4CIDRs) > 0
}
