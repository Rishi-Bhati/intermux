package shaping

import (
	"context"
	"fmt"
	"io"
	"math"
	"sync"
	"time"

	"golang.org/x/time/rate"
)

const (
	MinMbit = 0.064
	MaxMbit = 100_000.0

	minBurstBytes = 16 * 1024
	maxBurstBytes = 4 * 1024 * 1024
)

// ValidateMbit validates a limit in Mbit/s: nil or 0 means unlimited (returns nil).
func ValidateMbit(v *float64) (*float64, error) {
	if v == nil || *v == 0 {
		return nil, nil
	}
	val := *v
	if math.IsNaN(val) || math.IsInf(val, 0) || val < MinMbit || val > MaxMbit {
		return nil, fmt.Errorf("a limit must be between %g and %g Mbit/s", MinMbit, MaxMbit)
	}
	out := val
	return &out, nil
}

// DynamicLimiter enforces a reconfigurable token-bucket byte rate limit without dropping active flows.
type DynamicLimiter struct {
	mu         sync.Mutex
	lim        *rate.Limiter
	bytesPerS  float64
	burst      int
	mbit       *float64
	reconfigCh chan struct{}
}

// NewDynamicLimiter returns an unlimited DynamicLimiter unless an initial limit is set.
func NewDynamicLimiter() *DynamicLimiter {
	return &DynamicLimiter{
		lim:        rate.NewLimiter(rate.Inf, minBurstBytes),
		reconfigCh: make(chan struct{}),
	}
}

// SetLimitMbit dynamically updates the rate limit; nil or 0 removes the cap.
func (d *DynamicLimiter) SetLimitMbit(mbit *float64) error {
	clean, err := ValidateMbit(mbit)
	if err != nil {
		return err
	}

	d.mu.Lock()
	defer d.mu.Unlock()

	if clean == nil {
		d.lim.SetLimit(rate.Inf)
		d.lim.SetBurst(minBurstBytes)
		d.bytesPerS = 0
		d.burst = minBurstBytes
		d.mbit = nil
	} else {
		bps := *clean * 1_000_000.0 / 8.0
		burst := int(bps * 0.02)
		if burst < minBurstBytes {
			burst = minBurstBytes
		}
		if burst > maxBurstBytes {
			burst = maxBurstBytes
		}
		d.lim.SetLimit(rate.Limit(bps))
		d.lim.SetBurst(burst)
		d.bytesPerS = bps
		d.burst = burst
		v := *clean
		d.mbit = &v
	}

	close(d.reconfigCh)
	d.reconfigCh = make(chan struct{})
	return nil
}

// CurrentMbit returns a copy of the configured limit in Mbit/s, or nil if unlimited.
func (d *DynamicLimiter) CurrentMbit() *float64 {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.mbit == nil {
		return nil
	}
	v := *d.mbit
	return &v
}

func dynamicSlice(bps float64, burst int) int {
	if bps <= 0 {
		return burst
	}
	if bps < 125_000 { // < 1 Mbit/s
		return 4096
	}
	if bps < 1_250_000 { // < 10 Mbit/s
		return 8192
	}
	return 32 * 1024
}

func (d *DynamicLimiter) waitTokens(ctx context.Context, n int) error {
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		d.mu.Lock()
		bps := d.bytesPerS
		lim := d.lim
		ch := d.reconfigCh
		d.mu.Unlock()

		if bps <= 0 {
			return nil
		}

		res := lim.ReserveN(time.Now(), n)
		if !res.OK() {
			return fmt.Errorf("shaping reservation exceeded burst (%d > %d)", n, lim.Burst())
		}
		delay := res.Delay()
		if delay <= 0 {
			return nil
		}
		timer := time.NewTimer(delay)
		select {
		case <-timer.C:
			return nil
		case <-ch:
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			res.Cancel()
		case <-ctx.Done():
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			res.Cancel()
			return ctx.Err()
		}
	}
}

// WaitStreamChunk paces a stream chunk of up to requested bytes, returning the permitted chunk size.
func (d *DynamicLimiter) WaitStreamChunk(ctx context.Context, requested int) (int, error) {
	if d == nil || requested <= 0 {
		return requested, nil
	}
	d.mu.Lock()
	bps := d.bytesPerS
	burst := d.burst
	d.mu.Unlock()
	if bps <= 0 {
		return requested, nil
	}
	n := requested
	if slice := dynamicSlice(bps, burst); n > slice {
		n = slice
	}
	if n > burst {
		n = burst
	}
	if err := d.waitTokens(ctx, n); err != nil {
		return 0, err
	}
	return n, nil
}

// WaitDatagram paces an entire UDP datagram atomically without splitting it.
func (d *DynamicLimiter) WaitDatagram(ctx context.Context, pktLen int) error {
	if d == nil || pktLen <= 0 {
		return nil
	}
	rem := pktLen
	for rem > 0 {
		d.mu.Lock()
		bps := d.bytesPerS
		burst := d.burst
		d.mu.Unlock()
		if bps <= 0 {
			return nil
		}
		chunk := rem
		if chunk > burst {
			chunk = burst
		}
		if err := d.waitTokens(ctx, chunk); err != nil {
			return err
		}
		rem -= chunk
	}
	return nil
}

type limitedReader struct {
	ctx     context.Context
	r       io.Reader
	lim     *DynamicLimiter
	onBytes func(int64)
}

func (l *limitedReader) Read(p []byte) (int, error) {
	n, err := l.r.Read(p)
	if n > 0 {
		if l.lim != nil {
			rem := n
			for rem > 0 {
				allowed, werr := l.lim.WaitStreamChunk(l.ctx, rem)
				if werr != nil {
					return n, werr
				}
				rem -= allowed
			}
		}
		if l.onBytes != nil {
			l.onBytes(int64(n))
		}
	}
	return n, err
}

// WrapReader wraps r with download pacing and byte accounting.
func WrapReader(ctx context.Context, r io.Reader, lim *DynamicLimiter, onBytes func(int64)) io.Reader {
	return &limitedReader{ctx: ctx, r: r, lim: lim, onBytes: onBytes}
}

type limitedWriter struct {
	ctx     context.Context
	w       io.Writer
	lim     *DynamicLimiter
	onBytes func(int64)
}

func (l *limitedWriter) Write(p []byte) (int, error) {
	if l.lim == nil {
		n, err := l.w.Write(p)
		if n > 0 && l.onBytes != nil {
			l.onBytes(int64(n))
		}
		return n, err
	}
	written := 0
	for written < len(p) {
		allowed, err := l.lim.WaitStreamChunk(l.ctx, len(p)-written)
		if err != nil {
			return written, err
		}
		n, werr := l.w.Write(p[written : written+allowed])
		if n > 0 {
			written += n
			if l.onBytes != nil {
				l.onBytes(int64(n))
			}
		}
		if werr != nil {
			return written, werr
		}
	}
	return written, nil
}

// WrapWriter wraps w with upload pacing and byte accounting.
func WrapWriter(ctx context.Context, w io.Writer, lim *DynamicLimiter, onBytes func(int64)) io.Writer {
	return &limitedWriter{ctx: ctx, w: w, lim: lim, onBytes: onBytes}
}

// GroupLimiter pairs Down and Up limiters for one bound interface.
type GroupLimiter struct {
	Down *DynamicLimiter
	Up   *DynamicLimiter
}

// LimitSnapshot is the serialized view of an interface's bandwidth caps.
type LimitSnapshot struct {
	Down *float64 `json:"down"`
	Up   *float64 `json:"up"`
}

// Manager holds per-interface GroupLimiters.
type Manager struct {
	mu     sync.RWMutex
	groups map[string]*GroupLimiter
}

// NewManager initializes a Manager.
func NewManager() *Manager {
	return &Manager{groups: make(map[string]*GroupLimiter)}
}

// GetOrCreate returns the GroupLimiter for iface, creating an unlimited one if needed.
func (m *Manager) GetOrCreate(iface string) *GroupLimiter {
	m.mu.RLock()
	g := m.groups[iface]
	m.mu.RUnlock()
	if g != nil {
		return g
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	g = m.groups[iface]
	if g == nil {
		g = &GroupLimiter{Down: NewDynamicLimiter(), Up: NewDynamicLimiter()}
		m.groups[iface] = g
	}
	return g
}

// SetLimit validates and applies down/up Mbit/s caps for iface.
func (m *Manager) SetLimit(iface string, down, up *float64) (LimitSnapshot, error) {
	cd, err := ValidateMbit(down)
	if err != nil {
		return LimitSnapshot{}, err
	}
	cu, err := ValidateMbit(up)
	if err != nil {
		return LimitSnapshot{}, err
	}
	g := m.GetOrCreate(iface)
	if err := g.Down.SetLimitMbit(cd); err != nil {
		return LimitSnapshot{}, err
	}
	if err := g.Up.SetLimitMbit(cu); err != nil {
		return LimitSnapshot{}, err
	}
	return LimitSnapshot{Down: cd, Up: cu}, nil
}

// Remove resets iface limits to unlimited and deletes its entry.
func (m *Manager) Remove(iface string) {
	m.mu.Lock()
	g := m.groups[iface]
	delete(m.groups, iface)
	m.mu.Unlock()
	if g != nil {
		_ = g.Down.SetLimitMbit(nil)
		_ = g.Up.SetLimitMbit(nil)
	}
}

// Snapshot returns active non-nil limits by interface name.
func (m *Manager) Snapshot() map[string]LimitSnapshot {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := make(map[string]LimitSnapshot)
	for iface, g := range m.groups {
		d := g.Down.CurrentMbit()
		u := g.Up.CurrentMbit()
		if d != nil || u != nil {
			out[iface] = LimitSnapshot{Down: d, Up: u}
		}
	}
	return out
}
