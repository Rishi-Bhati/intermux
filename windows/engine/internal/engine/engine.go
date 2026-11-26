package engine

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/xjasonlyu/tun2socks/v2/core"
	"github.com/xjasonlyu/tun2socks/v2/core/adapter"
	"github.com/xjasonlyu/tun2socks/v2/core/device/iobased"
	"gvisor.dev/gvisor/pkg/tcpip/stack"

	"github.com/Rishi-Bhati/intermux/windows/engine/internal/counters"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/dns"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/nics"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/pidmap"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/shaping"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/socks"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/tun"
)

const (
	Version             = "3.0.0-beta.1"
	MaxAdoptionsPerUser = 20
	DefaultVerifyURL    = "https://api.ipify.org"
)

var ErrInterfaceDown = errors.New("bound interface is down or unbindable")

// PIDResolver abstracts Windows IP Helper socket & process attribution for testing.
type PIDResolver interface {
	LookupTCP(srcIP netip.Addr, srcPort uint16, dstIP netip.Addr, dstPort uint16) (uint32, string, string, bool)
	LookupUDP(srcIP netip.Addr, srcPort uint16) (uint32, string, string, bool)
	InspectPID(pid uint32) (pidmap.ProcInfo, bool)
	SnapshotProcs() []pidmap.ProcInfo
	LivePIDs(pids []uint32) []uint32
	TerminatePIDs(pids []uint32, expectedSID string, force bool) ([]uint32, []uint32)
}

type BindingRecord struct {
	ID        string   `json:"id"`
	Iface     string   `json:"iface"`
	App       string   `json:"app"`
	AppName   string   `json:"app_name"`
	PIDs      []uint32 `json:"pids"`
	OwnerSID  string   `json:"owner_sid,omitempty"`
	CreatedAt string   `json:"created_at"`
}

type AdoptionRecord struct {
	ID        string   `json:"id"`
	Iface     string   `json:"iface"`
	App       string   `json:"app"`
	AppName   string   `json:"app_name"`
	PIDs      []uint32 `json:"pids"`
	OwnerSID  string   `json:"owner_sid,omitempty"`
	CreatedAt string   `json:"created_at"`
}

type VerifyResult struct {
	Iface    string  `json:"iface"`
	PublicIP *string `json:"public_ip"`
	Error    *string `json:"error"`
}

type SetDNSResult struct {
	Iface     string   `json:"iface"`
	Servers   []string `json:"servers"`
	Automatic bool     `json:"automatic"`
	Warnings  []string `json:"warnings"`
}

type Engine struct {
	mu          sync.RWMutex
	mode        string // "wintun" or "socks"
	enforceSID  bool
	startedAt   time.Time
	verifyURL   string
	nicProvider nics.Provider
	dialer      nics.OutboundDialer
	pidLookup   PIDResolver
	rules       *pidmap.RuleTable
	counters    *counters.Registry
	shaping     *shaping.Manager
	dnsMgr      *dns.Manager

	tunDev     tun.Device
	gvisorStk  *stack.Stack
	bindings   map[string]*BindingRecord
	adoptions  map[string]*AdoptionRecord
	socksSrvs  map[string]*socks.Server
	socksCreds map[string]socks.Credential

	ctx    context.Context
	cancel context.CancelFunc
}

type Config struct {
	Mode        string
	EnforceSID  bool
	NICProvider nics.Provider
	Dialer      nics.OutboundDialer
	PIDLookup   PIDResolver
	VerifyURL   string
}

func New(cfg Config) *Engine {
	if cfg.Mode == "" {
		cfg.Mode = "socks"
	}
	if cfg.NICProvider == nil {
		cfg.NICProvider = &nics.SystemProvider{ExcludeAdapterName: tun.DefaultAdapterName}
	}
	if cfg.Dialer == nil {
		cfg.Dialer = nics.NewSystemDialer()
	}
	if cfg.PIDLookup == nil {
		cfg.PIDLookup = pidmap.NewSystemLookup()
	}
	if cfg.VerifyURL == "" {
		cfg.VerifyURL = DefaultVerifyURL
	}
	ctx, cancel := context.WithCancel(context.Background())
	e := &Engine{
		mode:        cfg.Mode,
		enforceSID:  cfg.EnforceSID,
		startedAt:   time.Now(),
		verifyURL:   cfg.VerifyURL,
		nicProvider: cfg.NICProvider,
		dialer:      cfg.Dialer,
		pidLookup:   cfg.PIDLookup,
		rules:       pidmap.NewRuleTable(),
		counters:    counters.NewRegistry(),
		shaping:     shaping.NewManager(),
		dnsMgr:      dns.NewManager(cfg.Dialer),
		bindings:    make(map[string]*BindingRecord),
		adoptions:   make(map[string]*AdoptionRecord),
		socksSrvs:   make(map[string]*socks.Server),
		socksCreds:  make(map[string]socks.Credential),
		ctx:         ctx,
		cancel:      cancel,
	}
	go e.counters.Start(ctx)
	return e
}

// StartWintun initializes Wintun + gVisor + /1 routes and begins packet routing.
func (e *Engine) StartWintun(mtu uint32) error {
	var ex4, ex6 []netip.Prefix
	if list, err := e.nicProvider.List(e.ctx, true); err == nil {
		for _, nic := range list {
			for _, c := range nic.IPv4CIDRs {
				if p, err := netip.ParsePrefix(c); err == nil {
					ex4 = append(ex4, p)
				}
			}
			for _, c := range nic.IPv6CIDRs {
				if p, err := netip.ParsePrefix(c); err == nil {
					ex6 = append(ex6, p)
				}
			}
		}
	}
	dev, err := tun.Open(tun.Config{
		AdapterName: tun.DefaultAdapterName,
		MTU:         mtu,
		ExcludeV4:   ex4,
		ExcludeV6:   ex6,
		EnableIPv6:  true,
	})
	if err != nil {
		return err
	}
	ep, err := iobased.New(dev, dev.MTU(), 0)
	if err != nil {
		_ = dev.Close()
		return err
	}
	stk, err := core.CreateStack(&core.Config{
		LinkEndpoint:     ep,
		TransportHandler: e,
	})
	if err != nil {
		_ = dev.Close()
		return err
	}
	e.mu.Lock()
	e.tunDev = dev
	e.gvisorStk = stk
	e.mode = "wintun"
	e.mu.Unlock()

	go e.procSnapshotLoop()
	return nil
}

func (e *Engine) procSnapshotLoop() {
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-e.ctx.Done():
			return
		case <-ticker.C:
			if snap := e.pidLookup.SnapshotProcs(); len(snap) > 0 {
				e.rules.UpdateSnapshot(snap)
			}
		}
	}
}

// Close shuts down SOCKS5 listeners, gVisor stack, and Wintun adapter cleanly.
func (e *Engine) Close() error {
	e.cancel()
	e.mu.Lock()
	srvs := make([]*socks.Server, 0, len(e.socksSrvs))
	for _, s := range e.socksSrvs {
		srvs = append(srvs, s)
	}
	e.socksSrvs = make(map[string]*socks.Server)
	stk := e.gvisorStk
	e.gvisorStk = nil
	dev := e.tunDev
	e.tunDev = nil
	e.mu.Unlock()

	for _, s := range srvs {
		_ = s.Close()
	}
	if stk != nil {
		stk.Close()
	}
	if dev != nil {
		return dev.Close()
	}
	return nil
}

func (e *Engine) Mode() string      { e.mu.RLock(); defer e.mu.RUnlock(); return e.mode }
func (e *Engine) UptimeSec() float64 { return time.Since(e.startedAt).Seconds() }
func (e *Engine) TunUp() bool       { e.mu.RLock(); defer e.mu.RUnlock(); return e.tunDev != nil }

func (e *Engine) ListInterfaces(includeAll bool) ([]nics.NICInfo, error) {
	return e.nicProvider.List(e.ctx, includeAll)
}

func (e *Engine) Status() map[string]any {
	e.mu.RLock()
	defer e.mu.RUnlock()

	bindings := make([]*BindingRecord, 0, len(e.bindings))
	for _, b := range e.bindings {
		bindings = append(bindings, b)
	}
	adoptions := make([]*AdoptionRecord, 0, len(e.adoptions))
	for _, a := range e.adoptions {
		adoptions = append(adoptions, a)
	}
	socksPorts := make(map[string]int, len(e.socksSrvs))
	for k, s := range e.socksSrvs {
		socksPorts[k] = s.Port()
	}
	return map[string]any{
		"mode":          e.mode,
		"tun_up":        e.tunDev != nil,
		"bindings":      bindings,
		"adoptions":     adoptions,
		"dns":           e.dnsMgr.Snapshot(),
		"limits":        e.shaping.Snapshot(),
		"socks_proxies": socksPorts,
	}
}

func genHexID(nbytes int) string {
	b := make([]byte, nbytes)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func (e *Engine) ensureSocksForIface(nic nics.NICInfo, bindingID string) (int, *socks.Credential, error) {
	e.mu.Lock()
	defer e.mu.Unlock()
	if srv, ok := e.socksSrvs[nic.Name]; ok {
		cred := e.socksCreds[nic.Name]
		return srv.Port(), &cred, nil
	}
	cred, err := socks.GenerateCredential()
	if err != nil {
		return 0, nil, err
	}
	srv, err := socks.StartServer(e.ctx, "127.0.0.1:0", nic.Name, nic.IfIndex, bindingID, cred, e.dialer, e.counters, e.shaping, e.dnsMgr)
	if err != nil {
		return 0, nil, err
	}
	e.socksSrvs[nic.Name] = srv
	e.socksCreds[nic.Name] = cred
	return srv.Port(), &cred, nil
}

// BindRegister registers a bound application/PIDs on iface. Enforces same-SID ownership when enforceSID=true.
func (e *Engine) BindRegister(
	id, iface, app string,
	pids []uint32,
	callerSID string,
	dnsMode any,
	limitDown, limitUp *float64,
	wantSocks bool,
) (map[string]any, error) {
	nic, err := e.nicProvider.GetByName(e.ctx, iface)
	if err != nil {
		return nil, fmt.Errorf("not_found: %w", err)
	}
	if !nic.Up || !nic.Bindable {
		return nil, fmt.Errorf("not_bindable: interface %s is not up or bindable", iface)
	}
	if id == "" {
		id = "bind-" + genHexID(4)
	}
	rule := pidmap.RouteRule{
		BindingID: id,
		Iface:     nic.Name,
		AppPath:   app,
		OwnerSID:  callerSID,
	}
	for _, pid := range pids {
		var procSID string
		if e.enforceSID {
			info, ok := e.pidLookup.InspectPID(pid)
			if !ok {
				return nil, fmt.Errorf("not_found: PID %d not found", pid)
			}
			procSID = info.UserSID
		}
		if err := e.rules.RegisterPID(pid, procSID, rule, e.enforceSID); err != nil {
			return nil, err
		}
	}
	if app != "" {
		e.rules.RegisterExe(app, rule)
	}

	var warnings []string
	if dnsMode != nil && dnsMode != dns.Keep {
		res, derr := e.SetDNS(nic.Name, dnsMode)
		if derr != nil {
			return nil, derr
		}
		warnings = append(warnings, res.Warnings...)
	}
	if limitDown != nil || limitUp != nil {
		if _, lerr := e.shaping.SetLimit(nic.Name, limitDown, limitUp); lerr != nil {
			return nil, lerr
		}
	}

	rec := &BindingRecord{
		ID:        id,
		Iface:     nic.Name,
		App:       app,
		AppName:   filepath.Base(app),
		PIDs:      append([]uint32(nil), pids...),
		OwnerSID:  callerSID,
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
	}
	e.mu.Lock()
	e.bindings[id] = rec
	e.mu.Unlock()
	e.counters.EnsureInterface(nic.Name)
	e.counters.EnsureBinding(id)

	var socksPort any
	var socksCred any
	if wantSocks || e.Mode() == "socks" {
		port, cred, serr := e.ensureSocksForIface(nic, id)
		if serr != nil {
			return nil, serr
		}
		socksPort = port
		socksCred = cred
	}
	return map[string]any{
		"binding":    rec,
		"socks_port": socksPort,
		"socks_auth": socksCred,
		"warnings":   warnings,
	}, nil
}

// BindUnregister removes a binding by id or (iface, app), checking callerSID ownership.
func (e *Engine) BindUnregister(id, iface, app, callerSID string) (map[string]any, error) {
	e.mu.Lock()
	var target *BindingRecord
	if id != "" {
		target = e.bindings[id]
	} else {
		for _, b := range e.bindings {
			if b.Iface == iface && strings.EqualFold(b.App, app) {
				target = b
				break
			}
		}
	}
	if target == nil {
		e.mu.Unlock()
		return map[string]any{"removed": false, "id": id}, nil
	}
	if e.enforceSID && callerSID != "" && target.OwnerSID != "" && !strings.EqualFold(target.OwnerSID, callerSID) {
		e.mu.Unlock()
		return nil, errors.New("permission_denied: binding belongs to another user")
	}
	delete(e.bindings, target.ID)
	e.mu.Unlock()

	e.rules.UnregisterBinding(target.ID)
	e.counters.RemoveBinding(target.ID)
	return map[string]any{"removed": true, "id": target.ID}, nil
}

// AdoptPIDs shifts already-running PIDs (and same-SID children) onto iface without restarting.
func (e *Engine) AdoptPIDs(id, iface, app string, pids []uint32, callerSID string) (map[string]any, error) {
	if len(pids) == 0 {
		return nil, errors.New("invalid_request: at least one PID is required")
	}
	nic, err := e.nicProvider.GetByName(e.ctx, iface)
	if err != nil {
		return nil, fmt.Errorf("not_found: %w", err)
	}
	if !nic.Up || !nic.Bindable {
		return nil, fmt.Errorf("not_bindable: interface %s is not up or bindable", iface)
	}
	if id == "" {
		id = genHexID(4)
	}

	e.mu.RLock()
	if _, exists := e.adoptions[id]; exists {
		e.mu.RUnlock()
		return nil, fmt.Errorf("conflict: adoption %s already exists", id)
	}
	userCount := 0
	for _, a := range e.adoptions {
		if callerSID == "" || strings.EqualFold(a.OwnerSID, callerSID) {
			userCount++
		}
	}
	e.mu.RUnlock()
	if userCount >= MaxAdoptionsPerUser {
		return nil, fmt.Errorf("no_slot: maximum %d adoptions per user reached", MaxAdoptionsPerUser)
	}

	rule := pidmap.RouteRule{
		BindingID: id,
		Iface:     nic.Name,
		AppPath:   app,
		OwnerSID:  callerSID,
		IsAdopt:   true,
	}
	for _, pid := range pids {
		var procSID string
		if e.enforceSID {
			info, ok := e.pidLookup.InspectPID(pid)
			if !ok {
				return nil, fmt.Errorf("not_found: PID %d not found", pid)
			}
			procSID = info.UserSID
		}
		if err := e.rules.RegisterPID(pid, procSID, rule, e.enforceSID); err != nil {
			e.rules.UnregisterBinding(id)
			return nil, err
		}
	}

	rec := &AdoptionRecord{
		ID:        id,
		Iface:     nic.Name,
		App:       app,
		AppName:   filepath.Base(app),
		PIDs:      append([]uint32(nil), pids...),
		OwnerSID:  callerSID,
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
	}
	e.mu.Lock()
	e.adoptions[id] = rec
	e.mu.Unlock()
	e.counters.EnsureInterface(nic.Name)
	e.counters.EnsureBinding(id)

	return map[string]any{
		"adoption":   rec,
		"moved_pids": pids,
		"warnings":   []string{},
	}, nil
}

// ReleaseAdoption stops routing an adopted group back to default.
func (e *Engine) ReleaseAdoption(id, callerSID string) (map[string]any, error) {
	e.mu.Lock()
	rec := e.adoptions[id]
	if rec == nil {
		e.mu.Unlock()
		return nil, fmt.Errorf("not_found: adoption %s not found", id)
	}
	if e.enforceSID && callerSID != "" && rec.OwnerSID != "" && !strings.EqualFold(rec.OwnerSID, callerSID) {
		e.mu.Unlock()
		return nil, errors.New("permission_denied: adoption belongs to another user")
	}
	delete(e.adoptions, id)
	e.mu.Unlock()

	e.rules.UnregisterBinding(id)
	e.counters.RemoveBinding(id)
	still := e.pidLookup.LivePIDs(rec.PIDs)
	return map[string]any{
		"id":            id,
		"released":      rec.PIDs,
		"still_running": still,
	}, nil
}

// SetDNS sets DNS servers for iface ("auto" or 1..4 IPv4 addresses) and probes reachability.
func (e *Engine) SetDNS(iface string, dnsSpec any) (SetDNSResult, error) {
	nic, err := e.nicProvider.GetByName(e.ctx, iface)
	if err != nil {
		return SetDNSResult{}, fmt.Errorf("not_found: %w", err)
	}
	automatic := false
	var candidates []string
	switch v := dnsSpec.(type) {
	case string:
		if v != dns.Auto {
			return SetDNSResult{}, fmt.Errorf("invalid_request: unsupported dns value %q", v)
		}
		automatic = true
		seen := make(map[string]struct{})
		for _, s := range append(append([]string(nil), nic.DNSServers...), dns.PublicFallback...) {
			norm, verr := dns.ValidateServer(s)
			if verr != nil {
				continue
			}
			if _, ok := seen[norm]; !ok {
				seen[norm] = struct{}{}
				candidates = append(candidates, norm)
			}
		}
		if len(candidates) > dns.MaxServers {
			candidates = candidates[:dns.MaxServers]
		}
	case []string:
		candidates, err = dns.ValidateServers(v)
		if err != nil {
			return SetDNSResult{}, fmt.Errorf("invalid_request: %w", err)
		}
	default:
		return SetDNSResult{}, errors.New("invalid_request: dns must be 'auto' or server list")
	}

	var warnings []string
	ordered := candidates
	if nic.Up && nic.IfIndex != 0 {
		ans := e.dnsMgr.ProbeServers(e.ctx, nic.IfIndex, candidates, 1500*time.Millisecond)
		if len(ans) == 0 {
			warnings = append(warnings, "no DNS server answered through this interface yet; name lookups may fail")
		} else {
			ordered = dns.OrderServers(candidates, ans)
		}
	}
	warnings = append(warnings, "Windows dnscache (svchost.exe) shares system getaddrinfo() cache across processes; direct-DNS and DoH apps get strict per-binding isolation")
	e.dnsMgr.SetPolicy(nic.Name, ordered, automatic)
	return SetDNSResult{
		Iface:     nic.Name,
		Servers:   ordered,
		Automatic: automatic,
		Warnings:  warnings,
	}, nil
}

// SetLimit configures dynamic bidirectional bandwidth caps in Mbit/s.
func (e *Engine) SetLimit(iface string, down, up *float64) (map[string]any, error) {
	snap, err := e.shaping.SetLimit(iface, down, up)
	if err != nil {
		return nil, fmt.Errorf("invalid_request: %w", err)
	}
	return map[string]any{
		"iface": iface,
		"down":  snap.Down,
		"up":    snap.Up,
	}, nil
}

// Counters returns a filtered snapshot of live counters.
func (e *Engine) Counters(ifaces []string, bindings []string, pids []uint32) counters.Snapshot {
	snap := e.counters.Snapshot()
	if len(ifaces) > 0 {
		keep := make(map[string]counters.DirectionStats, len(ifaces))
		for _, k := range ifaces {
			if v, ok := snap.Interfaces[k]; ok {
				keep[k] = v
			}
		}
		snap.Interfaces = keep
	}
	if len(bindings) > 0 {
		keep := make(map[string]counters.DirectionStats, len(bindings))
		for _, k := range bindings {
			if v, ok := snap.Bindings[k]; ok {
				keep[k] = v
			}
		}
		snap.Bindings = keep
	}
	if len(pids) > 0 {
		keep := make(map[uint32]counters.DirectionStats, len(pids))
		for _, k := range pids {
			if v, ok := snap.PIDs[k]; ok {
				keep[k] = v
			}
		}
		snap.PIDs = keep
	}
	return snap
}

// Verify checks public IPv4 visibility over the pinned target interface (8s timeout).
func (e *Engine) Verify(iface string) (VerifyResult, error) {
	nic, err := e.nicProvider.GetByName(e.ctx, iface)
	if err != nil {
		return VerifyResult{}, fmt.Errorf("not_found: %w", err)
	}
	if !nic.Up || !nic.Bindable {
		msg := "interface is not up or bindable"
		return VerifyResult{Iface: nic.Name, Error: &msg}, nil
	}
	client := &http.Client{
		Timeout: 8 * time.Second,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
				return e.dialer.DialTCP(ctx, nic.IfIndex, "tcp4", addr)
			},
		},
	}
	resp, err := client.Get(e.verifyURL)
	if err != nil {
		msg := err.Error()
		return VerifyResult{Iface: nic.Name, Error: &msg}, nil
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 128))
	if err != nil {
		msg := err.Error()
		return VerifyResult{Iface: nic.Name, Error: &msg}, nil
	}
	ipStr := strings.TrimSpace(string(raw))
	addr, err := netip.ParseAddr(ipStr)
	if err != nil || !addr.Is4() {
		msg := fmt.Sprintf("invalid public IP response: %q", ipStr)
		return VerifyResult{Iface: nic.Name, Error: &msg}, nil
	}
	ip := addr.String()
	return VerifyResult{Iface: nic.Name, PublicIP: &ip}, nil
}

// Reconnect refreshes interface lists and restarts any stale SOCKS listeners whose iface changed.
func (e *Engine) Reconnect() (map[string]any, error) {
	list, err := e.nicProvider.List(e.ctx, false)
	if err != nil {
		return nil, err
	}
	var refreshed []string
	for _, n := range list {
		if n.Up && n.Bindable {
			refreshed = append(refreshed, n.Name)
		}
	}
	return map[string]any{
		"refreshed":      refreshed,
		"tun_reattached": e.TunUp(),
	}, nil
}

// TeardownIdle removes bindings and adoptions whose registered PIDs have all exited.
func (e *Engine) TeardownIdle() map[string]any {
	e.mu.Lock()
	var removed []string
	activeIfaces := make(map[string]bool)

	for id, b := range e.bindings {
		if len(b.PIDs) > 0 && len(e.pidLookup.LivePIDs(b.PIDs)) == 0 {
			delete(e.bindings, id)
			e.rules.UnregisterBinding(id)
			e.counters.RemoveBinding(id)
			removed = append(removed, id)
		} else {
			activeIfaces[b.Iface] = true
		}
	}
	for id, a := range e.adoptions {
		if len(a.PIDs) > 0 && len(e.pidLookup.LivePIDs(a.PIDs)) == 0 {
			delete(e.adoptions, id)
			e.rules.UnregisterBinding(id)
			e.counters.RemoveBinding(id)
			removed = append(removed, id)
		} else {
			activeIfaces[a.Iface] = true
		}
	}

	var tornDown []string
	for iface, srv := range e.socksSrvs {
		if !activeIfaces[iface] {
			_ = srv.Close()
			delete(e.socksSrvs, iface)
			delete(e.socksCreds, iface)
			e.shaping.Remove(iface)
			e.dnsMgr.ClearPolicy(iface)
			tornDown = append(tornDown, iface)
		}
	}
	e.mu.Unlock()
	return map[string]any{
		"removed":           removed,
		"torn_down_ifaces": tornDown,
	}
}

// Reset removes all bindings, adoptions, shaping, DNS, and optionally kills owned processes.
func (e *Engine) Reset(killApps bool, callerSID string) map[string]any {
	e.mu.Lock()
	var pidsToKill []uint32
	remBindings := len(e.bindings)
	remAdoptions := len(e.adoptions)
	for _, b := range e.bindings {
		pidsToKill = append(pidsToKill, b.PIDs...)
	}
	for _, a := range e.adoptions {
		pidsToKill = append(pidsToKill, a.PIDs...)
	}
	e.bindings = make(map[string]*BindingRecord)
	e.adoptions = make(map[string]*AdoptionRecord)
	for iface, srv := range e.socksSrvs {
		_ = srv.Close()
		delete(e.socksSrvs, iface)
		delete(e.socksCreds, iface)
		e.shaping.Remove(iface)
		e.dnsMgr.ClearPolicy(iface)
	}
	e.mu.Unlock()

	e.rules.Clear()
	e.counters.Reset()

	var stillRunning []uint32
	if killApps && len(pidsToKill) > 0 {
		_, stillRunning = e.pidLookup.TerminatePIDs(pidsToKill, callerSID, true)
	}
	return map[string]any{
		"removed_bindings":  remBindings,
		"removed_adoptions": remAdoptions,
		"still_running":     stillRunning,
		"routes_restored":   true,
	}
}

// resolveFlow resolves (ifIndex, ifaceName, bindingID, isBound, err) for an arriving Wintun flow.
func (e *Engine) resolveFlow(pid uint32, exe, userSID string) (uint32, string, string, bool, error) {
	if rule, ok := e.rules.ResolvePID(pid, exe, userSID, e.pidLookup.InspectPID); ok {
		nic, err := e.nicProvider.GetByName(e.ctx, rule.Iface)
		if err != nil || !nic.Up || !nic.Bindable {
			return 0, rule.Iface, rule.BindingID, true, ErrInterfaceDown
		}
		return nic.IfIndex, nic.Name, rule.BindingID, true, nil
	}
	def, err := e.nicProvider.DefaultPhysical(e.ctx)
	if err != nil {
		return 0, "", "", false, err
	}
	return def.IfIndex, def.Name, "", false, nil
}

func addrFromBytes(raw []byte) netip.Addr {
	if len(raw) == 4 {
		return netip.AddrFrom4([4]byte(raw))
	}
	if len(raw) == 16 {
		return netip.AddrFrom16([16]byte(raw))
	}
	return netip.Addr{}
}

// HandleTCP implements tun2socks adapter.TransportHandler for arriving TCP connections.
func (e *Engine) HandleTCP(conn adapter.TCPConn) {
	defer conn.Close()
	id := conn.ID()
	srcIP := addrFromBytes(id.RemoteAddress.AsSlice())
	dstIP := addrFromBytes(id.LocalAddress.AsSlice())
	srcPort := id.RemotePort
	dstPort := id.LocalPort

	pid, exe, sid, _ := e.pidLookup.LookupTCP(srcIP, srcPort, dstIP, dstPort)
	ifIndex, ifaceName, bindingID, isBound, err := e.resolveFlow(pid, exe, sid)
	if err != nil {
		return // kill-switch drop if bound interface is down
	}

	if dstPort == 53 {
		upstreams := e.dnsMgr.ServersFor(ifaceName, nil)
		_ = e.dnsMgr.ForwardTCP(e.ctx, conn, ifIndex, upstreams)
		return
	}

	netName := "tcp4"
	if dstIP.Is6() {
		netName = "tcp6"
	}
	target := net.JoinHostPort(dstIP.String(), strconv.Itoa(int(dstPort)))
	dialCtx, cancel := context.WithTimeout(e.ctx, 15*time.Second)
	remote, err := e.dialer.DialTCP(dialCtx, ifIndex, netName, target)
	cancel()
	if err != nil {
		return
	}
	defer remote.Close()

	flow := e.counters.FlowStart(ifaceName, bindingID, pid)
	defer flow.Close()

	var downLim, upLim *shaping.DynamicLimiter
	if isBound {
		grp := e.shaping.GetOrCreate(ifaceName)
		downLim = grp.Down
		upLim = grp.Up
	}

	done := make(chan struct{}, 2)
	go func() {
		defer func() { done <- struct{}{} }()
		r := shaping.WrapReader(e.ctx, remote, downLim, flow.AddRX)
		_, _ = io.Copy(conn, r)
	}()
	go func() {
		defer func() { done <- struct{}{} }()
		w := shaping.WrapWriter(e.ctx, remote, upLim, flow.AddTX)
		_, _ = io.Copy(w, conn)
	}()
	<-done
	<-done
}

// HandleUDP implements tun2socks adapter.TransportHandler for arriving UDP flows.
func (e *Engine) HandleUDP(conn adapter.UDPConn) {
	defer conn.Close()
	id := conn.ID()
	srcIP := addrFromBytes(id.RemoteAddress.AsSlice())
	srcPort := id.RemotePort
	dstIP := addrFromBytes(id.LocalAddress.AsSlice())
	dstPort := id.LocalPort

	pid, exe, sid, _ := e.pidLookup.LookupUDP(srcIP, srcPort)
	ifIndex, ifaceName, bindingID, isBound, err := e.resolveFlow(pid, exe, sid)
	if err != nil {
		return
	}

	if dstPort == 53 {
		upstreams := e.dnsMgr.ServersFor(ifaceName, nil)
		buf := make([]byte, 4096)
		_ = conn.SetDeadline(time.Now().Add(5 * time.Second))
		n, _, rerr := conn.ReadFrom(buf)
		if rerr != nil {
			return
		}
		reply, ferr := e.dnsMgr.ForwardUDP(e.ctx, buf[:n], ifIndex, upstreams)
		if ferr == nil {
			_, _ = conn.Write(reply)
		}
		return
	}

	netName := "udp4"
	if dstIP.Is6() {
		netName = "udp6"
	}
	remote, err := e.dialer.ListenUDP(e.ctx, ifIndex, netName)
	if err != nil {
		return
	}
	defer remote.Close()
	dstAddr := &net.UDPAddr{IP: dstIP.AsSlice(), Port: int(dstPort)}

	flow := e.counters.FlowStart(ifaceName, bindingID, pid)
	defer flow.Close()

	var downLim, upLim *shaping.DynamicLimiter
	if isBound {
		grp := e.shaping.GetOrCreate(ifaceName)
		downLim = grp.Down
		upLim = grp.Up
	}

	idleTimeout := 60 * time.Second
	done := make(chan struct{}, 2)
	go func() {
		defer func() { done <- struct{}{} }()
		buf := make([]byte, 65535)
		for {
			_ = remote.SetReadDeadline(time.Now().Add(idleTimeout))
			n, _, err := remote.ReadFrom(buf)
			if err != nil {
				return
			}
			if downLim != nil {
				if err := downLim.WaitDatagram(e.ctx, n); err != nil {
					return
				}
			}
			if _, err := conn.Write(buf[:n]); err != nil {
				return
			}
			flow.AddRX(int64(n))
		}
	}()
	go func() {
		defer func() { done <- struct{}{} }()
		buf := make([]byte, 65535)
		for {
			_ = conn.SetReadDeadline(time.Now().Add(idleTimeout))
			n, _, err := conn.ReadFrom(buf)
			if err != nil {
				return
			}
			if upLim != nil {
				if err := upLim.WaitDatagram(e.ctx, n); err != nil {
					return
				}
			}
			if _, err := remote.WriteTo(buf[:n], dstAddr); err != nil {
				return
			}
			flow.AddTX(int64(n))
		}
	}()
	<-done
}
