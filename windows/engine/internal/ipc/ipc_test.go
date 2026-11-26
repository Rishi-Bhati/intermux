package ipc

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"net"
	"net/netip"
	"strings"
	"testing"

	"github.com/Rishi-Bhati/intermux/windows/engine/internal/engine"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/nics"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/pidmap"
)

type stubProvider struct{}

func (stubProvider) List(ctx context.Context, includeAll bool) ([]nics.NICInfo, error) {
	return []nics.NICInfo{{Name: "Wi-Fi", IfIndex: 7, Kind: nics.KindWifi, Up: true, Bindable: true, DefaultRoute: true}}, nil
}
func (stubProvider) GetByName(ctx context.Context, name string) (nics.NICInfo, error) {
	return nics.NICInfo{Name: "Wi-Fi", IfIndex: 7, Kind: nics.KindWifi, Up: true, Bindable: true, DefaultRoute: true}, nil
}
func (stubProvider) DefaultPhysical(ctx context.Context) (nics.NICInfo, error) {
	return nics.NICInfo{Name: "Wi-Fi", IfIndex: 7, Kind: nics.KindWifi, Up: true, Bindable: true, DefaultRoute: true}, nil
}

type stubResolver struct{}

func (stubResolver) LookupTCP(netip.Addr, uint16, netip.Addr, uint16) (uint32, string, string, bool) {
	return 0, "", "", false
}
func (stubResolver) LookupUDP(netip.Addr, uint16) (uint32, string, string, bool) { return 0, "", "", false }
func (stubResolver) InspectPID(pid uint32) (pidmap.ProcInfo, bool) {
	if pid == 1001 {
		return pidmap.ProcInfo{PID: 1001, UserSID: "S-1-5-21-1-1001"}, true
	}
	if pid == 1002 {
		return pidmap.ProcInfo{PID: 1002, UserSID: "S-1-5-21-1-1002"}, true
	}
	return pidmap.ProcInfo{}, false
}
func (stubResolver) SnapshotProcs() []pidmap.ProcInfo                        { return nil }
func (stubResolver) LivePIDs(pids []uint32) []uint32                         { return pids }
func (stubResolver) TerminatePIDs([]uint32, string, bool) ([]uint32, []uint32) { return nil, nil }

func TestSecurityPipeSDDLAndDACLSafety(t *testing.T) {
	sid := "S-1-5-21-123456-7890-1001"
	sddl, err := BuildPipeSDDL(sid)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"D:P", "(A;;GA;;;SY)", "(A;;GA;;;BA)", "(A;;GA;;;" + sid + ")"} {
		if !strings.Contains(sddl, want) {
			t.Fatalf("SDDL %q missing %q", sddl, want)
		}
	}
	for _, forbid := range []string{"WD", "AU", "IU", "NU"} {
		if strings.Contains(sddl, forbid) {
			t.Fatalf("SDDL %q must NOT contain %q", sddl, forbid)
		}
	}

	// Non-admin writable directory must be refused.
	badEntries := []ACLEntry{
		{SID: SIDSystem, AccessMask: 0x10000000, IsAllow: true},
		{SID: SIDAdministrators, AccessMask: 0x10000000, IsAllow: true},
		{SID: "S-1-5-32-545", AccessMask: 0x0002, IsAllow: true}, // BUILTIN\Users FILE_WRITE_DATA
	}
	if err := CheckDACLSafety(`C:\Users\bob\AppData\Local\InterMux`, badEntries); err == nil {
		t.Fatal("expected non-admin-writable directory to be refused")
	}

	// Admin-only directory passes (Users read-only 0x1200a9 has no write mask).
	goodEntries := []ACLEntry{
		{SID: SIDSystem, AccessMask: 0x1F01FF, IsAllow: true},
		{SID: SIDAdministrators, AccessMask: 0x1F01FF, IsAllow: true},
		{SID: "S-1-5-32-545", AccessMask: 0x1200A9, IsAllow: true}, // Read & Execute only
	}
	if err := CheckDACLSafety(`C:\Program Files\InterMux`, goodEntries); err != nil {
		t.Fatalf("expected admin-only directory to pass, got: %v", err)
	}
}

func TestHandlerValidationAndCrossSIDRefusal(t *testing.T) {
	eng := engine.New(engine.Config{
		Mode:        "wintun",
		EnforceSID:  true,
		NICProvider: stubProvider{},
		PIDLookup:   stubResolver{},
	})
	defer eng.Close()
	h := NewHandler(eng, `C:\Program Files\InterMux\intermux-net.exe`, nil)

	// Unknown top-level field rejected.
	r := h.Handle([]byte(`{"v":1,"action":"ping","params":{},"extra":1}`), "S-1-5-21-1-1001")
	if r.OK || r.Error.Code != "invalid_request" {
		t.Fatalf("expected invalid_request, got %+v", r)
	}

	// Cross-SID bind_register rejected.
	r = h.Handle([]byte(`{"v":1,"action":"bind_register","params":{"iface":"Wi-Fi","app":"C:\\app.exe","pids":[1002]}}`), "S-1-5-21-1-1001")
	if r.OK || r.Error.Code != "permission_denied" {
		t.Fatalf("expected permission_denied on cross-SID PID, got %+v", r)
	}

	// Same-SID bind_register + set_limit + counters + status + shutdown succeed.
	for _, payload := range []string{
		`{"v":1,"action":"ping","params":{}}`,
		`{"v":1,"action":"interfaces","params":{}}`,
		`{"v":1,"action":"bind_register","params":{"id":"b1","iface":"Wi-Fi","app":"C:\\app.exe","pids":[1001]}}`,
		`{"v":1,"action":"set_limit","params":{"iface":"Wi-Fi","down":10.5,"up":null}}`,
		`{"v":1,"action":"counters","params":{}}`,
		`{"v":1,"action":"status","params":{}}`,
		`{"v":1,"action":"bind_unregister","params":{"id":"b1"}}`,
	} {
		resp := h.Handle([]byte(payload), "S-1-5-21-1-1001")
		if !resp.OK {
			t.Fatalf("action %s failed: %+v", payload, resp.Error)
		}
	}
}

func TestLoopbackTCPAuthAndStdio(t *testing.T) {
	eng := engine.New(engine.Config{NICProvider: stubProvider{}, PIDLookup: stubResolver{}})
	defer eng.Close()
	h := NewHandler(eng, `C:\Program Files\InterMux\intermux-net.exe`, nil)

	var out bytes.Buffer
	if !ServeStdio(strings.NewReader("{\"v\":1,\"action\":\"ping\",\"params\":{}}\n"), &out, "S-1-5-21-1-1001", h, false) {
		t.Fatalf("ServeStdio failed: %s", out.String())
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	srv, err := StartLoopback(ctx, "127.0.0.1:0", "", "S-1-5-21-1-1001", h)
	if err != nil {
		t.Fatal(err)
	}

	// Bad token rejected.
	c1, err := net.Dial("tcp", srv.Addr())
	if err != nil {
		t.Fatal(err)
	}
	_, _ = c1.Write([]byte("{\"v\":1,\"auth\":\"wrong\"}\n"))
	line, _ := bufio.NewReader(c1).ReadBytes('\n')
	var errResp Response
	_ = json.Unmarshal(line, &errResp)
	if errResp.OK || errResp.Error == nil || errResp.Error.Code != "permission_denied" {
		t.Fatalf("expected permission_denied for bad auth, got %s", line)
	}
	c1.Close()

	// Valid token succeeds.
	c2, err := net.Dial("tcp", srv.Addr())
	if err != nil {
		t.Fatal(err)
	}
	defer c2.Close()
	rd := bufio.NewReader(c2)
	_, _ = c2.Write([]byte(`{"v":1,"auth":"` + srv.Token() + `"}` + "\n"))
	_, _ = rd.ReadBytes('\n')
	_, _ = c2.Write([]byte(`{"v":1,"action":"ping","params":{}}` + "\n"))
	pingLine, _ := rd.ReadBytes('\n')
	var okResp Response
	_ = json.Unmarshal(pingLine, &okResp)
	if !okResp.OK {
		t.Fatalf("expected ping OK, got %s", pingLine)
	}
}
