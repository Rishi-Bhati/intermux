package pidmap

import "testing"

func TestCrossSIDRegistrationRefused(t *testing.T) {
	rt := NewRuleTable()
	err := rt.RegisterPID(4242, "S-1-5-21-999-1002", RouteRule{
		BindingID: "b1",
		Iface:     "Wi-Fi",
		OwnerSID:  "S-1-5-21-999-1001",
	}, true)
	if err == nil {
		t.Fatal("expected cross-SID PID registration to be refused")
	}

	if err := rt.RegisterPID(4242, "S-1-5-21-999-1001", RouteRule{
		BindingID: "b1",
		Iface:     "Wi-Fi",
		OwnerSID:  "S-1-5-21-999-1001",
	}, true); err != nil {
		t.Fatalf("same-SID registration failed: %v", err)
	}
}

func TestTreeInheritanceRequiresSameSID(t *testing.T) {
	rt := NewRuleTable()
	if err := rt.RegisterPID(100, "S-1-5-21-1-1001", RouteRule{
		BindingID: "b1",
		Iface:     "Wi-Fi",
		OwnerSID:  "S-1-5-21-1-1001",
	}, true); err != nil {
		t.Fatal(err)
	}

	rt.UpdateSnapshot([]ProcInfo{
		{PID: 100, PPID: 1, Exe: `C:\Apps\browser.exe`, UserSID: "S-1-5-21-1-1001"},
		{PID: 101, PPID: 100, Exe: `C:\Apps\helper.exe`, UserSID: "S-1-5-21-1-1001"},
		{PID: 102, PPID: 100, Exe: `C:\Apps\other_user.exe`, UserSID: "S-1-5-21-1-1002"},
	})

	if r, ok := rt.ResolvePID(101, `C:\Apps\helper.exe`, "S-1-5-21-1-1001", nil); !ok || r.Iface != "Wi-Fi" {
		t.Fatalf("same-SID child failed to inherit rule: ok=%v rule=%+v", ok, r)
	}
	if r, ok := rt.ResolvePID(102, `C:\Apps\other_user.exe`, "S-1-5-21-1-1002", nil); ok {
		t.Fatalf("different-SID child unexpectedly inherited rule: %+v", r)
	}
}
