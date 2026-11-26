package tun

import (
	"fmt"
	"net/netip"
)

// PickSubnets selects collision-free IPv4 /24 and IPv6 /64 prefixes for the Wintun adapter.
func PickSubnets(excludeV4 []netip.Prefix, excludeV6 []netip.Prefix) (netip.Prefix, netip.Prefix, error) {
	v4Candidates := make([]netip.Prefix, 0, 560)
	// Prefer 198.19.0.0/24..198.19.254.0/24 first (avoids 198.18.0.0/24 used by Clash/sing-box).
	for b := 0; b <= 254; b++ {
		v4Candidates = append(v4Candidates, netip.PrefixFrom(netip.AddrFrom4([4]byte{198, 19, byte(b), 1}), 24))
	}
	for b := 1; b <= 254; b++ {
		v4Candidates = append(v4Candidates, netip.PrefixFrom(netip.AddrFrom4([4]byte{198, 18, byte(b), 1}), 24))
	}
	for b := 1; b <= 64; b++ {
		v4Candidates = append(v4Candidates, netip.PrefixFrom(netip.AddrFrom4([4]byte{100, 120, byte(b), 1}), 24))
	}
	for b := 200; b <= 254; b++ {
		v4Candidates = append(v4Candidates, netip.PrefixFrom(netip.AddrFrom4([4]byte{172, 31, byte(b), 1}), 24))
	}

	var chosenV4 netip.Prefix
	for _, cand := range v4Candidates {
		masked := cand.Masked()
		overlap := false
		for _, ex := range excludeV4 {
			if masked.Overlaps(ex.Masked()) {
				overlap = true
				break
			}
		}
		if !overlap {
			chosenV4 = cand
			break
		}
	}
	if !chosenV4.IsValid() {
		return netip.Prefix{}, netip.Prefix{}, fmt.Errorf("no free IPv4 TUN subnet available")
	}

	var chosenV6 netip.Prefix
	for n := 0; n < 256; n++ {
		addr := netip.AddrFrom16([16]byte{0xfd, 0x00, 0x69, 0x6d, 0x78, byte(n), 0, 0, 0, 0, 0, 0, 0, 0, 0, 1})
		cand := netip.PrefixFrom(addr, 64)
		masked := cand.Masked()
		overlap := false
		for _, ex := range excludeV6 {
			if masked.Overlaps(ex.Masked()) {
				overlap = true
				break
			}
		}
		if !overlap {
			chosenV6 = cand
			break
		}
	}
	if !chosenV6.IsValid() {
		return netip.Prefix{}, netip.Prefix{}, fmt.Errorf("no free IPv6 TUN subnet available")
	}
	return chosenV4, chosenV6, nil
}
