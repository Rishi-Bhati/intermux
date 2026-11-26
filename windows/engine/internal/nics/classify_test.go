package nics

import "testing"

func TestClassifyKindAndBindable(t *testing.T) {
	cases := []struct {
		ifType uint32
		name   string
		desc   string
		want   Kind
	}{
		{ifTypeIEEE80211, "Wi-Fi", "Intel(R) Wi-Fi 6E AX210", KindWifi},
		{ifTypeEthernetCSMACD, "Ethernet", "Realtek PCIe GbE Family Controller", KindEthernet},
		{ifTypePropVirtual, "WireGuard Tunnel", "WireGuard Tunnel", KindVPN},
		{ifTypeEthernetCSMACD, "OpenVPN TAP", "TAP-Windows Adapter V9", KindVPN},
		{ifTypeEthernetCSMACD, "vEthernet (Default Switch)", "Hyper-V Virtual Ethernet Adapter", KindVirtual},
		{ifTypeEthernetCSMACD, "USB Tether", "Remote NDIS based Internet Sharing Device", KindUSB},
		{ifTypeWWANPP, "Cellular", "Qualcomm Mobile Broadband", KindMobile},
	}
	for _, tc := range cases {
		got := ClassifyKind(tc.ifType, tc.name, tc.desc)
		if got != tc.want {
			t.Fatalf("ClassifyKind(%d, %q, %q) = %s, want %s", tc.ifType, tc.name, tc.desc, got, tc.want)
		}
	}

	if !IsBindable(KindVPN, true, []string{"10.8.0.2/24"}, false) {
		t.Fatal("expected active VPN without default gateway to be bindable")
	}
	if IsBindable(KindVirtual, true, []string{"172.20.0.1/20"}, true) {
		t.Fatal("expected virtual switch to be non-bindable")
	}
	if IsBindable(KindWifi, false, []string{"192.168.1.10/24"}, true) {
		t.Fatal("expected down adapter to be non-bindable")
	}
}
