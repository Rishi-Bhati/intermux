package counters

import (
	"context"
	"sync"
	"sync/atomic"
	"time"
)

// DirectionStats holds cumulative byte counts, 1 Hz byte/sec rates, and active flow counts.
type DirectionStats struct {
	RXBytes     uint64  `json:"rx_bytes"`
	TXBytes     uint64  `json:"tx_bytes"`
	RXBps       float64 `json:"rx_bps"`
	TXBps       float64 `json:"tx_bps"`
	ActiveFlows int64   `json:"active_flows"`
}

// Snapshot is an isolated point-in-time view of all counter buckets.
type Snapshot struct {
	TimestampUnixMS int64                     `json:"t_ms"`
	Interfaces      map[string]DirectionStats `json:"interfaces"`
	Bindings        map[string]DirectionStats `json:"bindings"`
	PIDs            map[uint32]DirectionStats `json:"pids"`
}

type atomicBucket struct {
	rxBytes     atomic.Uint64
	txBytes     atomic.Uint64
	activeFlows atomic.Int64

	lastRX uint64
	lastTX uint64
	rxBps  float64
	txBps  float64
}

func (b *atomicBucket) snapshot() DirectionStats {
	return DirectionStats{
		RXBytes:     b.rxBytes.Load(),
		TXBytes:     b.txBytes.Load(),
		RXBps:       b.rxBps,
		TXBps:       b.txBps,
		ActiveFlows: b.activeFlows.Load(),
	}
}

// FlowTracker tracks a single active TCP connection or UDP session.
type FlowTracker struct {
	iface   *atomicBucket
	binding *atomicBucket
	pid     *atomicBucket
	closed  atomic.Bool
}

// AddRX records n downloaded bytes (remote -> local app).
func (f *FlowTracker) AddRX(n int64) {
	if f == nil || n <= 0 {
		return
	}
	u := uint64(n)
	if f.iface != nil {
		f.iface.rxBytes.Add(u)
	}
	if f.binding != nil {
		f.binding.rxBytes.Add(u)
	}
	if f.pid != nil {
		f.pid.rxBytes.Add(u)
	}
}

// AddTX records n uploaded bytes (local app -> remote).
func (f *FlowTracker) AddTX(n int64) {
	if f == nil || n <= 0 {
		return
	}
	u := uint64(n)
	if f.iface != nil {
		f.iface.txBytes.Add(u)
	}
	if f.binding != nil {
		f.binding.txBytes.Add(u)
	}
	if f.pid != nil {
		f.pid.txBytes.Add(u)
	}
}

// Close decrements active_flows once; safe to call multiple times.
func (f *FlowTracker) Close() {
	if f == nil || !f.closed.CompareAndSwap(false, true) {
		return
	}
	if f.iface != nil {
		f.iface.activeFlows.Add(-1)
	}
	if f.binding != nil {
		f.binding.activeFlows.Add(-1)
	}
	if f.pid != nil {
		f.pid.activeFlows.Add(-1)
	}
}

// Registry aggregates atomic counters across interfaces, bindings, and PIDs.
type Registry struct {
	mu       sync.RWMutex
	ifaces   map[string]*atomicBucket
	bindings map[string]*atomicBucket
	pids     map[uint32]*atomicBucket
	lastAt   time.Time
}

// NewRegistry initializes an empty Registry.
func NewRegistry() *Registry {
	return &Registry{
		ifaces:   make(map[string]*atomicBucket),
		bindings: make(map[string]*atomicBucket),
		pids:     make(map[uint32]*atomicBucket),
	}
}

func (r *Registry) getOrCreateIface(name string) *atomicBucket {
	if name == "" {
		return nil
	}
	b := r.ifaces[name]
	if b == nil {
		b = &atomicBucket{}
		r.ifaces[name] = b
	}
	return b
}

func (r *Registry) getOrCreateBinding(id string) *atomicBucket {
	if id == "" {
		return nil
	}
	b := r.bindings[id]
	if b == nil {
		b = &atomicBucket{}
		r.bindings[id] = b
	}
	return b
}

func (r *Registry) getOrCreatePID(pid uint32) *atomicBucket {
	if pid == 0 {
		return nil
	}
	b := r.pids[pid]
	if b == nil {
		b = &atomicBucket{}
		r.pids[pid] = b
	}
	return b
}

// FlowStart registers an active flow on the interface, optional binding ID, and optional PID.
func (r *Registry) FlowStart(iface, bindingID string, pid uint32) *FlowTracker {
	r.mu.Lock()
	ib := r.getOrCreateIface(iface)
	bb := r.getOrCreateBinding(bindingID)
	pb := r.getOrCreatePID(pid)
	if ib != nil {
		ib.activeFlows.Add(1)
	}
	if bb != nil {
		bb.activeFlows.Add(1)
	}
	if pb != nil {
		pb.activeFlows.Add(1)
	}
	r.mu.Unlock()
	return &FlowTracker{iface: ib, binding: bb, pid: pb}
}

// EnsureInterface ensures a bucket exists for iface even before traffic starts.
func (r *Registry) EnsureInterface(iface string) {
	if iface == "" {
		return
	}
	r.mu.Lock()
	_ = r.getOrCreateIface(iface)
	r.mu.Unlock()
}

// EnsureBinding ensures a bucket exists for bindingID even before traffic starts.
func (r *Registry) EnsureBinding(bindingID string) {
	if bindingID == "" {
		return
	}
	r.mu.Lock()
	_ = r.getOrCreateBinding(bindingID)
	r.mu.Unlock()
}

// Start runs a 1 Hz sampler loop until ctx is cancelled.
func (r *Registry) Start(ctx context.Context) {
	r.SampleAt(time.Now())
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-ticker.C:
			r.SampleAt(now)
		}
	}
}

// SampleAt computes per-bucket RX/TX rates using the delta since the previous SampleAt call.
func (r *Registry) SampleAt(now time.Time) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if r.lastAt.IsZero() {
		r.lastAt = now
		for _, b := range r.ifaces {
			b.lastRX = b.rxBytes.Load()
			b.lastTX = b.txBytes.Load()
		}
		for _, b := range r.bindings {
			b.lastRX = b.rxBytes.Load()
			b.lastTX = b.txBytes.Load()
		}
		for _, b := range r.pids {
			b.lastRX = b.rxBytes.Load()
			b.lastTX = b.txBytes.Load()
		}
		return
	}

	elapsed := now.Sub(r.lastAt).Seconds()
	if elapsed <= 0 {
		return
	}
	r.lastAt = now

	update := func(b *atomicBucket) {
		rx := b.rxBytes.Load()
		tx := b.txBytes.Load()
		var dRX, dTX uint64
		if rx >= b.lastRX {
			dRX = rx - b.lastRX
		} else {
			dRX = rx
		}
		if tx >= b.lastTX {
			dTX = tx - b.lastTX
		} else {
			dTX = tx
		}
		b.lastRX = rx
		b.lastTX = tx
		b.rxBps = float64(dRX) / elapsed
		b.txBps = float64(dTX) / elapsed
	}

	for _, b := range r.ifaces {
		update(b)
	}
	for _, b := range r.bindings {
		update(b)
	}
	for _, b := range r.pids {
		update(b)
	}
}

// Snapshot returns a copy of current cumulative totals, rates, and active flow counts.
func (r *Registry) Snapshot() Snapshot {
	r.mu.RLock()
	defer r.mu.RUnlock()

	snap := Snapshot{
		TimestampUnixMS: time.Now().UnixMilli(),
		Interfaces:      make(map[string]DirectionStats, len(r.ifaces)),
		Bindings:        make(map[string]DirectionStats, len(r.bindings)),
		PIDs:            make(map[uint32]DirectionStats, len(r.pids)),
	}
	for k, b := range r.ifaces {
		snap.Interfaces[k] = b.snapshot()
	}
	for k, b := range r.bindings {
		snap.Bindings[k] = b.snapshot()
	}
	for k, b := range r.pids {
		snap.PIDs[k] = b.snapshot()
	}
	return snap
}

// PrunePID removes a PID bucket if it has zero active flows.
func (r *Registry) PrunePID(pid uint32) {
	r.mu.Lock()
	if b := r.pids[pid]; b != nil && b.activeFlows.Load() <= 0 {
		delete(r.pids, pid)
	}
	r.mu.Unlock()
}

// RemoveBinding removes a binding bucket.
func (r *Registry) RemoveBinding(id string) {
	r.mu.Lock()
	delete(r.bindings, id)
	r.mu.Unlock()
}

// Reset clears all tracked buckets.
func (r *Registry) Reset() {
	r.mu.Lock()
	r.ifaces = make(map[string]*atomicBucket)
	r.bindings = make(map[string]*atomicBucket)
	r.pids = make(map[uint32]*atomicBucket)
	r.lastAt = time.Time{}
	r.mu.Unlock()
}
