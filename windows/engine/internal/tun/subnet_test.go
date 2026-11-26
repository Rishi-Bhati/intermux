package tun

import (
	"net/netip"
	"testing"
)

func TestPickSubnetsAvoidsCollisions(t *testing.T) {
	ex4 := []netip.Prefix{
		netip.MustParsePrefix("198.18.0.0/16"),
		netip.MustParsePrefix("198.19.0.0/24"),
		netip.MustParsePrefix("198.19.1.0/24"),
	}
	ex6 := []netip.Prefix{
		netip.MustParsePrefix("fd00:696d:7800::/64"),
	}
	v4, v6, err := PickSubnets(ex4, ex6)
	if err != nil {
		t.Fatal(err)
	}
	if v4.Addr().String() != "198.19.2.1" || v4.Bits() != 24 {
		t.Fatalf("unexpected v4 subnet: %s", v4)
	}
	if v6.Addr().String() != "fd00:696d:7801::1" || v6.Bits() != 64 {
		t.Fatalf("unexpected v6 subnet: %s", v6)
	}
}
