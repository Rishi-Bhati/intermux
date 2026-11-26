package counters

import (
	"sync"
	"testing"
	"time"
)

func TestConcurrentFlowAccountingRace(t *testing.T) {
	reg := NewRegistry()
	var wg sync.WaitGroup
	const workers = 32
	const iters = 200

	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			ft := reg.FlowStart("Wi-Fi", "bind-1", uint32(1000+id))
			for i := 0; i < iters; i++ {
				ft.AddRX(100)
				ft.AddTX(50)
			}
			ft.Close()
			ft.Close() // idempotent
		}(w)
	}
	wg.Wait()

	snap := reg.Snapshot()
	got := snap.Interfaces["Wi-Fi"]
	wantRX := uint64(workers * iters * 100)
	wantTX := uint64(workers * iters * 50)
	if got.RXBytes != wantRX || got.TXBytes != wantTX {
		t.Fatalf("unexpected totals: got (%d, %d), want (%d, %d)", got.RXBytes, got.TXBytes, wantRX, wantTX)
	}
	if got.ActiveFlows != 0 {
		t.Fatalf("expected 0 active flows, got %d", got.ActiveFlows)
	}
	if snap.Bindings["bind-1"].RXBytes != wantRX {
		t.Fatalf("unexpected binding RX: %d", snap.Bindings["bind-1"].RXBytes)
	}
}

func TestSampleAtRateCalculation(t *testing.T) {
	reg := NewRegistry()
	t0 := time.Unix(1700000000, 0)
	ft := reg.FlowStart("Ethernet", "b1", 42)
	reg.SampleAt(t0)

	ft.AddRX(250_000)
	ft.AddTX(125_000)
	reg.SampleAt(t0.Add(1 * time.Second))

	snap := reg.Snapshot()
	st := snap.Interfaces["Ethernet"]
	if st.RXBps != 250_000 || st.TXBps != 125_000 {
		t.Fatalf("unexpected 1s rate: got (%.1f, %.1f)", st.RXBps, st.TXBps)
	}

	ft.AddRX(300_000)
	reg.SampleAt(t0.Add(2500 * time.Millisecond))
	st = reg.Snapshot().Interfaces["Ethernet"]
	if st.RXBps != 200_000 {
		t.Fatalf("expected 200000 RXBps over 1.5s, got %.1f", st.RXBps)
	}
}

func TestSnapshotIsolation(t *testing.T) {
	reg := NewRegistry()
	ft := reg.FlowStart("Wi-Fi", "b1", 7)
	ft.AddRX(1234)
	snap := reg.Snapshot()
	snap.Interfaces["Wi-Fi"] = DirectionStats{RXBytes: 999999}
	snap2 := reg.Snapshot()
	if snap2.Interfaces["Wi-Fi"].RXBytes != 1234 {
		t.Fatalf("snapshot mutation leaked into registry")
	}
	ft.Close()
}
