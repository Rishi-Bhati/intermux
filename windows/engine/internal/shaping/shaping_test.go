package shaping

import (
	"bytes"
	"context"
	"io"
	"math"
	"testing"
	"time"
)

func ptr(v float64) *float64 { return &v }

func TestValidateMbit(t *testing.T) {
	if got, err := ValidateMbit(nil); err != nil || got != nil {
		t.Fatalf("nil limit failed: got=%v err=%v", got, err)
	}
	if got, err := ValidateMbit(ptr(0)); err != nil || got != nil {
		t.Fatalf("zero limit failed: got=%v err=%v", got, err)
	}
	for _, ok := range []float64{MinMbit, 1.5, 25.0, MaxMbit} {
		got, err := ValidateMbit(ptr(ok))
		if err != nil || got == nil || *got != ok {
			t.Fatalf("valid limit %v rejected: got=%v err=%v", ok, got, err)
		}
	}
	for _, bad := range []float64{-1, 0.063, 100_000.001, math.NaN(), math.Inf(1)} {
		if _, err := ValidateMbit(ptr(bad)); err == nil {
			t.Fatalf("expected error for invalid limit %v", bad)
		}
	}
}

func TestStreamRateEnforcement(t *testing.T) {
	lim := NewDynamicLimiter()
	// 2.0 Mbit/s = 250,000 B/s. Transferring 128 KiB after initial 16 KiB burst should take ~0.45s.
	if err := lim.SetLimitMbit(ptr(2.0)); err != nil {
		t.Fatal(err)
	}
	payload := bytes.Repeat([]byte("A"), 128*1024)
	var dst bytes.Buffer
	var counted int64
	w := WrapWriter(context.Background(), &dst, lim, func(n int64) { counted += n })

	start := time.Now()
	n, err := w.Write(payload)
	elapsed := time.Since(start)
	if err != nil || n != len(payload) {
		t.Fatalf("write failed: n=%d err=%v", n, err)
	}
	if counted != int64(len(payload)) || dst.Len() != len(payload) {
		t.Fatalf("byte mismatch: counted=%d dst=%d", counted, dst.Len())
	}
	if elapsed < 300*time.Millisecond {
		t.Fatalf("rate limiter did not throttle sufficiently: elapsed=%v", elapsed)
	}
}

func TestDynamicSetLimitNoDrop(t *testing.T) {
	lim := NewDynamicLimiter()
	if err := lim.SetLimitMbit(ptr(0.064)); err != nil {
		t.Fatal(err)
	}
	// Drain initial burst (16 KiB) so next wait blocks.
	if _, err := lim.WaitStreamChunk(context.Background(), 16*1024); err != nil {
		t.Fatal(err)
	}

	done := make(chan error, 1)
	go func() {
		_, err := io.Copy(io.Discard, WrapReader(context.Background(), bytes.NewReader(make([]byte, 32*1024)), lim, nil))
		done <- err
	}()

	time.Sleep(30 * time.Millisecond)
	startWake := time.Now()
	if err := lim.SetLimitMbit(nil); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("transfer failed after unlimiting: %v", err)
		}
		if time.Since(startWake) > 250*time.Millisecond {
			t.Fatalf("dynamic unlimit did not wake promptly: %v", time.Since(startWake))
		}
	case <-time.After(2 * time.Second):
		t.Fatal("transfer hung after SetLimitMbit(nil)")
	}
}

func TestUDPDatagramPreservation(t *testing.T) {
	lim := NewDynamicLimiter()
	if err := lim.SetLimitMbit(ptr(1.0)); err != nil {
		t.Fatal(err)
	}
	// 24 KiB datagram exceeds 16 KiB burst; WaitDatagram must pay slices without error.
	if err := lim.WaitDatagram(context.Background(), 24*1024); err != nil {
		t.Fatalf("WaitDatagram failed: %v", err)
	}
}
