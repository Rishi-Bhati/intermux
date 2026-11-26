package pidmap

import (
	"fmt"
	"net/netip"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// ProcInfo describes an OS process with its owning Windows user SID.
type ProcInfo struct {
	PID       uint32
	PPID      uint32
	Exe       string
	UserSID   string
	StartTime uint64
}

// RouteRule maps a bound/adopted application or PID to a target interface.
type RouteRule struct {
	BindingID string
	Iface     string
	AppPath   string
	OwnerSID  string
	IsAdopt   bool
}

// NormalizeExe canonicalizes an executable path for case-insensitive Windows matching.
func NormalizeExe(path string) string {
	cleaned := strings.TrimSpace(path)
	if cleaned == "" {
		return ""
	}
	cleaned = strings.ReplaceAll(cleaned, "/", `\`)
	return strings.ToLower(filepath.Clean(cleaned))
}

// FlowKey uniquely identifies a local L4 endpoint originating a packet on Wintun.
type FlowKey struct {
	Proto   uint8
	SrcIP   netip.Addr
	SrcPort uint16
}

type flowCacheEntry struct {
	pid       uint32
	exe       string
	userSID   string
	expiresAt time.Time
}

// FlowCache caches recent 3-tuple socket->PID lookups to avoid repeated IP Helper table scans.
type FlowCache struct {
	mu      sync.RWMutex
	entries map[FlowKey]flowCacheEntry
}

// NewFlowCache returns an initialized FlowCache.
func NewFlowCache() *FlowCache {
	return &FlowCache{entries: make(map[FlowKey]flowCacheEntry)}
}

func (c *FlowCache) Get(k FlowKey, now time.Time) (uint32, string, string, bool) {
	c.mu.RLock()
	e, ok := c.entries[k]
	c.mu.RUnlock()
	if !ok || now.After(e.expiresAt) {
		return 0, "", "", false
	}
	return e.pid, e.exe, e.userSID, true
}

func (c *FlowCache) Put(k FlowKey, pid uint32, exe, userSID string, ttl time.Duration, now time.Time) {
	c.mu.Lock()
	if len(c.entries) > 8192 {
		for fk, ev := range c.entries {
			if now.After(ev.expiresAt) {
				delete(c.entries, fk)
			}
		}
	}
	c.entries[k] = flowCacheEntry{pid: pid, exe: exe, userSID: userSID, expiresAt: now.Add(ttl)}
	c.mu.Unlock()
}

// RuleTable resolves OS PIDs and process trees (with strict same-SID isolation) to RouteRules.
type RuleTable struct {
	mu        sync.RWMutex
	pidRules  map[uint32]RouteRule
	exeRules  map[string]RouteRule
	inherited map[uint32]RouteRule
	procs     map[uint32]ProcInfo
}

// NewRuleTable returns an empty RuleTable.
func NewRuleTable() *RuleTable {
	return &RuleTable{
		pidRules:  make(map[uint32]RouteRule),
		exeRules:  make(map[string]RouteRule),
		inherited: make(map[uint32]RouteRule),
		procs:     make(map[uint32]ProcInfo),
	}
}

// RegisterPID registers an explicit PID rule after verifying ownerSID matches procSID (when enforceSID is true).
func (rt *RuleTable) RegisterPID(pid uint32, procSID string, rule RouteRule, enforceSID bool) error {
	if pid == 0 {
		return fmt.Errorf("invalid PID 0")
	}
	if enforceSID {
		if rule.OwnerSID == "" || procSID == "" || !strings.EqualFold(rule.OwnerSID, procSID) {
			return fmt.Errorf("permission_denied: cross-SID PID registration refused (pid=%d owner=%q proc=%q)", pid, rule.OwnerSID, procSID)
		}
	}
	rt.mu.Lock()
	rt.pidRules[pid] = rule
	rt.inherited[pid] = rule
	rt.mu.Unlock()
	return nil
}

// RegisterExe registers an executable path rule scoped to OwnerSID.
func (rt *RuleTable) RegisterExe(appPath string, rule RouteRule) {
	norm := NormalizeExe(appPath)
	if norm == "" {
		return
	}
	rule.AppPath = norm
	rt.mu.Lock()
	rt.exeRules[norm] = rule
	rt.mu.Unlock()
}

// UnregisterBinding removes all PID, exe, and inherited rules for bindingID.
func (rt *RuleTable) UnregisterBinding(bindingID string) {
	rt.mu.Lock()
	for pid, r := range rt.pidRules {
		if r.BindingID == bindingID {
			delete(rt.pidRules, pid)
		}
	}
	for exe, r := range rt.exeRules {
		if r.BindingID == bindingID {
			delete(rt.exeRules, exe)
		}
	}
	for pid, r := range rt.inherited {
		if r.BindingID == bindingID {
			delete(rt.inherited, pid)
		}
	}
	rt.mu.Unlock()
}

// Clear removes all rules.
func (rt *RuleTable) Clear() {
	rt.mu.Lock()
	rt.pidRules = make(map[uint32]RouteRule)
	rt.exeRules = make(map[string]RouteRule)
	rt.inherited = make(map[uint32]RouteRule)
	rt.mu.Unlock()
}

// UpdateSnapshot refreshes the process tree snapshot and propagates same-SID inheritance.
func (rt *RuleTable) UpdateSnapshot(snapshot []ProcInfo) {
	rt.mu.Lock()
	defer rt.mu.Unlock()

	procMap := make(map[uint32]ProcInfo, len(snapshot))
	for _, p := range snapshot {
		if p.Exe != "" {
			p.Exe = NormalizeExe(p.Exe)
		}
		procMap[p.PID] = p
	}
	rt.procs = procMap

	// Prune dead PID rules only when process snapshot is non-empty and confirms exit.
	if len(procMap) > 0 {
		for pid := range rt.pidRules {
			if _, alive := procMap[pid]; !alive {
				delete(rt.pidRules, pid)
			}
		}
	}

	newInherited := make(map[uint32]RouteRule, len(rt.pidRules))
	for pid, rule := range rt.pidRules {
		newInherited[pid] = rule
	}

	for pid, info := range procMap {
		if rule, ok := rt.resolveLocked(pid, info, procMap); ok {
			newInherited[pid] = rule
		}
	}
	rt.inherited = newInherited
}

func sameSID(ruleOwner, procSID string) bool {
	if ruleOwner == "" {
		return true
	}
	return procSID != "" && strings.EqualFold(ruleOwner, procSID)
}

func (rt *RuleTable) resolveLocked(pid uint32, info ProcInfo, procMap map[uint32]ProcInfo) (RouteRule, bool) {
	if r, ok := rt.pidRules[pid]; ok && sameSID(r.OwnerSID, info.UserSID) {
		return r, true
	}
	if info.Exe != "" {
		if r, ok := rt.exeRules[NormalizeExe(info.Exe)]; ok && sameSID(r.OwnerSID, info.UserSID) {
			return r, true
		}
	}
	cur := info
	visited := make(map[uint32]struct{}, 16)
	for depth := 0; depth < 64 && cur.PPID != 0 && cur.PPID != cur.PID; depth++ {
		if _, seen := visited[cur.PPID]; seen {
			break
		}
		visited[cur.PPID] = struct{}{}
		parent, ok := procMap[cur.PPID]
		if !ok {
			break
		}
		if r, ok := rt.pidRules[parent.PID]; ok && sameSID(r.OwnerSID, info.UserSID) {
			return r, true
		}
		if parent.Exe != "" {
			if r, ok := rt.exeRules[NormalizeExe(parent.Exe)]; ok && sameSID(r.OwnerSID, info.UserSID) {
				return r, true
			}
		}
		cur = parent
	}
	return RouteRule{}, false
}

// ResolvePID resolves a PID (or on-demand lookup) to its matching RouteRule, enforcing same-SID matching.
func (rt *RuleTable) ResolvePID(pid uint32, exe, userSID string, lookup func(uint32) (ProcInfo, bool)) (RouteRule, bool) {
	if pid == 0 {
		return RouteRule{}, false
	}
	rt.mu.RLock()
	if r, ok := rt.pidRules[pid]; ok && sameSID(r.OwnerSID, userSID) {
		rt.mu.RUnlock()
		return r, true
	}
	if r, ok := rt.inherited[pid]; ok && sameSID(r.OwnerSID, userSID) {
		rt.mu.RUnlock()
		return r, true
	}
	if exe != "" {
		if r, ok := rt.exeRules[NormalizeExe(exe)]; ok && sameSID(r.OwnerSID, userSID) {
			rt.mu.RUnlock()
			return r, true
		}
	}
	procsCopy := rt.procs
	rt.mu.RUnlock()

	if lookup == nil {
		return RouteRule{}, false
	}

	curPID := pid
	visited := make(map[uint32]struct{}, 16)
	for depth := 0; depth < 32 && curPID != 0; depth++ {
		if _, seen := visited[curPID]; seen {
			break
		}
		visited[curPID] = struct{}{}
		info, ok := procsCopy[curPID]
		if !ok {
			info, ok = lookup(curPID)
			if !ok {
				break
			}
		}
		rt.mu.RLock()
		if r, ok := rt.pidRules[curPID]; ok && sameSID(r.OwnerSID, userSID) {
			rt.mu.RUnlock()
			rt.mu.Lock()
			rt.inherited[pid] = r
			rt.mu.Unlock()
			return r, true
		}
		if info.Exe != "" {
			if r, ok := rt.exeRules[NormalizeExe(info.Exe)]; ok && sameSID(r.OwnerSID, userSID) {
				rt.mu.RUnlock()
				rt.mu.Lock()
				rt.inherited[pid] = r
				rt.mu.Unlock()
				return r, true
			}
		}
		rt.mu.RUnlock()
		if info.PPID == 0 || info.PPID == curPID {
			break
		}
		curPID = info.PPID
	}
	return RouteRule{}, false
}
