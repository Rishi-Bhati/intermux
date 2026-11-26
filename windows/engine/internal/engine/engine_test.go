package engine

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"strings"
	"testing"

	"github.com/Rishi-Bhati/intermux/windows/engine/internal/nics"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/pidmap"
)

type fakeNICProvider struct {
	list []nics.NICInfo
}

func (f *fakeNICProvider) List(ctx context.Context, includeAll bool) ([]nics.NICInfo, error) {
	return f.list, nil
}

func (f *fakeNICProvider) GetByName(ctx context.Context, name string) (nics.NICInfo, error) {
	for _, n := range f.list {
		if strings.EqualFold(n.Name, name) {
			return n, nil
		}
	}
	return nics.NICInfo{}, fmt.Errorf("not found: %s", name)
}

func (f *fakeNICProvider) DefaultPhysical(ctx context.Context) (nics.NICInfo, error) {
	for _, n := range f.list {
		if n.DefaultRoute && n.Up {
			return n, nil
		}
	}
	return f.list[0], nil
}

type fakePIDResolver struct {
	procs map[uint32]pidmap.ProcInfo
}

func (f *fakePIDResolver) LookupTCP(srcIP netip.Addr, srcPort uint16, dstIP netip.Addr, dstPort uint16) (uint32, string, string, bool) {
	return 0, "", "", false
}
func (f *fakePIDResolver) LookupUDP(srcIP netip.Addr, srcPort uint16) (uint32, string, string, bool) {
	return 0, "", "", false
}
func (f *fakePIDResolver) InspectPID(pid uint32) (pidmap.ProcInfo, bool) {
	p, ok := f.procs[pid]
	return p, ok
}
func (f *fakePIDResolver) SnapshotProcs() []pidmap.ProcInfo {
	var out []pidmap.ProcInfo
	for _, p := range f.procs {
		out = append(out, p)
	}
	return out
}
func (f *fakePIDResolver) LivePIDs(pids []uint32) []uint32 {
	var live []uint32
	for _, pid := range pids {
		if _, ok := f.procs[pid]; ok {
			live = append(live, pid)
		}
	}
	return live
}
func (f *fakePIDResolver) TerminatePIDs(pids []uint32, expectedSID string, force bool) ([]uint32, []uint32) {
	return pids, nil
}

type recordingDialer struct {
	lastIfIndex uint32
}

func (d *recordingDialer) DialTCP(ctx context.Context, ifIndex uint32, network, addr string) (net.Conn, error) {
	d.lastIfIndex = ifIndex
	return (&net.Dialer{}).DialContext(ctx, network, addr)
}

func (d *recordingDialer) ListenUDP(ctx context.Context, ifIndex uint32, network string) (net.PacketConn, error) {
	d.lastIfIndex = ifIndex
	return net.ListenPacket("udp4", "127.0.0.1:0")
}

func TestFlowRoutingKillSwitchAndCrossSID(t *testing.T) {
	np := &fakeNICProvider{
		list: []nics.NICInfo{
			{Name: "Ethernet", IfIndex: 10, Kind: nics.KindEthernet, Up: true, Bindable: true, DefaultRoute: true},
			{Name: "Wi-Fi", IfIndex: 20, Kind: nics.KindWifi, Up: true, Bindable: true},
		},
	}
	pr := &fakePIDResolver{
		procs: map[uint32]pidmap.ProcInfo{
			1000: {PID: 1000, PPID: 1, Exe: `C:\Apps\app.exe`, UserSID: "S-1-5-21-1-1001"},
			1001: {PID: 1001, PPID: 1000, Exe: `C:\Apps\child.exe`, UserSID: "S-1-5-21-1-1001"},
			2000: {PID: 2000, PPID: 1, Exe: `C:\Apps\other.exe`, UserSID: "S-1-5-21-1-1002"},
		},
	}
	eng := New(Config{
		Mode:        "wintun",
		EnforceSID:  true,
		NICProvider: np,
		PIDLookup:   pr,
		Dialer:      &recordingDialer{},
	})
	defer eng.Close()

	// Cross-SID bind registration must be refused.
	if _, err := eng.BindRegister("b-bad", "Wi-Fi", `C:\Apps\other.exe`, []uint32{2000}, "S-1-5-21-1-1001", "keep", nil, nil, false); err == nil {
		t.Fatal("expected cross-SID BindRegister to fail")
	}

	// Same-SID bind registration succeeds and child inherits Wi-Fi (IfIndex 20).
	if _, err := eng.BindRegister("b1", "Wi-Fi", `C:\Apps\app.exe`, []uint32{1000}, "S-1-5-21-1-1001", "keep", nil, nil, false); err != nil {
		t.Fatalf("BindRegister failed: %v", err)
	}
	idx, iface, _, bound, err := eng.resolveFlow(1001, `C:\Apps\child.exe`, "S-1-5-21-1-1001")
	if err != nil || !bound || idx != 20 || iface != "Wi-Fi" {
		t.Fatalf("unexpected child flow resolution: idx=%d iface=%s bound=%v err=%v", idx, iface, bound, err)
	}

	// Unbound PID routes to DefaultPhysical (Ethernet, IfIndex 10).
	idx, iface, _, bound, err = eng.resolveFlow(9999, `C:\Apps\unbound.exe`, "S-1-5-21-1-1001")
	if err != nil || bound || idx != 10 || iface != "Ethernet" {
		t.Fatalf("unexpected unbound flow resolution: idx=%d iface=%s bound=%v err=%v", idx, iface, bound, err)
	}

	// Kill switch: bringing Wi-Fi down must fail closed, never fall back to Ethernet.
	np.list[1].Up = false
	if _, _, _, _, err := eng.resolveFlow(1000, `C:\Apps\app.exe`, "S-1-5-21-1-1001"); err == nil {
		t.Fatal("expected kill-switch error when bound interface goes down")
	}
}

func TestVerifyAndAdoptionLimit(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("203.0.113.42\n"))
	}))
	defer srv.Close()

	dialer := &recordingDialer{}
	np := &fakeNICProvider{
		list: []nics.NICInfo{
			{Name: "Wi-Fi", IfIndex: 25, Kind: nics.KindWifi, Up: true, Bindable: true, DefaultRoute: true},
		},
	}
	pr := &fakePIDResolver{
		procs: map[uint32]pidmap.ProcInfo{
			500: {PID: 500, PPID: 1, Exe: `C:\app.exe`, UserSID: "S-1-5-21-1-1001"},
		},
	}
	eng := New(Config{
		NICProvider: np,
		PIDLookup:   pr,
		Dialer:      dialer,
		EnforceSID:  true,
		VerifyURL:   srv.URL,
	})
	defer eng.Close()

	res, err := eng.Verify("Wi-Fi")
	if err != nil || res.PublicIP == nil || *res.PublicIP != "203.0.113.42" || dialer.lastIfIndex != 25 {
		t.Fatalf("Verify failed: res=%+v err=%v lastIf=%d", res, err, dialer.lastIfIndex)
	}

	for i := 0; i < MaxAdoptionsPerUser; i++ {
		if _, err := eng.AdoptPIDs("", "Wi-Fi", `C:\app.exe`, []uint32{500}, "S-1-5-21-1-1001"); err != nil {
			t.Fatalf("AdoptPIDs #%d failed: %v", i, err)
		}
	}
	if _, err := eng.AdoptPIDs("", "Wi-Fi", `C:\app.exe`, []uint32{500}, "S-1-5-21-1-1001"); err == nil {
		t.Fatal("expected 21st adoption to be rejected")
	}
}
