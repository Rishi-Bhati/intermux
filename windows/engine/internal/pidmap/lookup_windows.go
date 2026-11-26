//go:build windows

package pidmap

import (
	"encoding/binary"
	"errors"
	"net/netip"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	afINET  = 2
	afINET6 = 23

	tcpTableOwnerPIDAll = 5
	udpTableOwnerPID    = 1
)

var (
	iphlpapi                = syscall.NewLazyDLL("iphlpapi.dll")
	procGetExtendedTcpTable = iphlpapi.NewProc("GetExtendedTcpTable")
	procGetExtendedUdpTable = iphlpapi.NewProc("GetExtendedUdpTable")
)

type mibTCPRowOwnerPID struct {
	State      uint32
	LocalAddr  [4]byte
	LocalPort  uint32
	RemoteAddr [4]byte
	RemotePort uint32
	OwningPID  uint32
}

type mibTCP6RowOwnerPID struct {
	LocalAddr     [16]byte
	LocalScopeID  uint32
	LocalPort     uint32
	RemoteAddr    [16]byte
	RemoteScopeID uint32
	RemotePort    uint32
	State         uint32
	OwningPID     uint32
}

type mibUDPRowOwnerPID struct {
	LocalAddr [4]byte
	LocalPort uint32
	OwningPID uint32
}

type mibUDP6RowOwnerPID struct {
	LocalAddr    [16]byte
	LocalScopeID uint32
	LocalPort    uint32
	OwningPID    uint32
}

func ntohsPort(raw uint32) uint16 {
	var b [4]byte
	*(*uint32)(unsafe.Pointer(&b[0])) = raw
	return binary.BigEndian.Uint16(b[:2])
}

// SystemLookup implements Windows IP Helper socket attribution and process SID inspection.
type SystemLookup struct {
	bufPool sync.Pool
	cache   *FlowCache
}

// NewSystemLookup initializes a Windows SystemLookup.
func NewSystemLookup() *SystemLookup {
	return &SystemLookup{
		bufPool: sync.Pool{
			New: func() any {
				b := make([]byte, 64*1024)
				return &b
			},
		},
		cache: NewFlowCache(),
	}
}

func (s *SystemLookup) getTable(proc *syscall.LazyProc, family uint32, tableClass uint32) ([]byte, error) {
	bp := s.bufPool.Get().(*[]byte)
	buf := *bp
	defer func() {
		*bp = buf
		s.bufPool.Put(bp)
	}()

	size := uint32(len(buf))
	for attempt := 0; attempt < 4; attempt++ {
		r1, _, _ := proc.Call(
			uintptr(unsafe.Pointer(&buf[0])),
			uintptr(unsafe.Pointer(&size)),
			0, // bOrder = FALSE
			uintptr(family),
			uintptr(tableClass),
			0,
		)
		if r1 == 0 {
			out := make([]byte, size)
			copy(out, buf[:size])
			return out, nil
		}
		if syscall.Errno(r1) == windows.ERROR_INSUFFICIENT_BUFFER && size > uint32(len(buf)) {
			buf = make([]byte, size)
			continue
		}
		return nil, syscall.Errno(r1)
	}
	return nil, errors.New("IP Helper table buffer overflow")
}

// LookupTCP maps an arriving TUN TCP 4-tuple to (pid, exe, userSID, ok).
func (s *SystemLookup) LookupTCP(srcIP netip.Addr, srcPort uint16, dstIP netip.Addr, dstPort uint16) (uint32, string, string, bool) {
	key := FlowKey{Proto: 6, SrcIP: srcIP, SrcPort: srcPort}
	now := time.Now()
	if pid, exe, sid, ok := s.cache.Get(key, now); ok && pid != 0 {
		return pid, exe, sid, true
	}

	delays := []time.Duration{0, 100 * time.Microsecond, 500 * time.Microsecond}
	for _, d := range delays {
		if d > 0 {
			time.Sleep(d)
		}
		pid := s.scanTCP(srcIP, srcPort, dstIP, dstPort)
		if pid != 0 {
			info, _ := s.InspectPID(pid)
			s.cache.Put(key, pid, info.Exe, info.UserSID, 5*time.Second, time.Now())
			return pid, info.Exe, info.UserSID, true
		}
	}
	return 0, "", "", false
}

func (s *SystemLookup) scanTCP(srcIP netip.Addr, srcPort uint16, dstIP netip.Addr, dstPort uint16) uint32 {
	if srcIP.Is4() {
		raw, err := s.getTable(procGetExtendedTcpTable, afINET, tcpTableOwnerPIDAll)
		if err != nil || len(raw) < 4 {
			return 0
		}
		count := *(*uint32)(unsafe.Pointer(&raw[0]))
		rowSize := unsafe.Sizeof(mibTCPRowOwnerPID{})
		if uintptr(len(raw)) < 4+uintptr(count)*rowSize {
			return 0
		}
		var fallback uint32
		for i := uint32(0); i < count; i++ {
			row := (*mibTCPRowOwnerPID)(unsafe.Pointer(&raw[4+uintptr(i)*rowSize]))
			lPort := ntohsPort(row.LocalPort)
			if lPort != srcPort {
				continue
			}
			lAddr := netip.AddrFrom4(row.LocalAddr)
			rAddr := netip.AddrFrom4(row.RemoteAddr)
			rPort := ntohsPort(row.RemotePort)
			if lAddr == srcIP && rAddr == dstIP && rPort == dstPort {
				return row.OwningPID
			}
			if (lAddr == srcIP || lAddr.IsUnspecified()) && fallback == 0 {
				fallback = row.OwningPID
			}
		}
		return fallback
	}

	raw, err := s.getTable(procGetExtendedTcpTable, afINET6, tcpTableOwnerPIDAll)
	if err != nil || len(raw) < 4 {
		return 0
	}
	count := *(*uint32)(unsafe.Pointer(&raw[0]))
	rowSize := unsafe.Sizeof(mibTCP6RowOwnerPID{})
	if uintptr(len(raw)) < 4+uintptr(count)*rowSize {
		return 0
	}
	var fallback uint32
	for i := uint32(0); i < count; i++ {
		row := (*mibTCP6RowOwnerPID)(unsafe.Pointer(&raw[4+uintptr(i)*rowSize]))
		lPort := ntohsPort(row.LocalPort)
		if lPort != srcPort {
			continue
		}
		lAddr := netip.AddrFrom16(row.LocalAddr)
		rAddr := netip.AddrFrom16(row.RemoteAddr)
		rPort := ntohsPort(row.RemotePort)
		if lAddr == srcIP && rAddr == dstIP && rPort == dstPort {
			return row.OwningPID
		}
		if (lAddr == srcIP || lAddr.IsUnspecified()) && fallback == 0 {
			fallback = row.OwningPID
		}
	}
	return fallback
}

// LookupUDP maps an arriving TUN UDP endpoint to (pid, exe, userSID, ok), matching 0.0.0.0/:: wildcards.
func (s *SystemLookup) LookupUDP(srcIP netip.Addr, srcPort uint16) (uint32, string, string, bool) {
	key := FlowKey{Proto: 17, SrcIP: srcIP, SrcPort: srcPort}
	now := time.Now()
	if pid, exe, sid, ok := s.cache.Get(key, now); ok && pid != 0 {
		return pid, exe, sid, true
	}

	delays := []time.Duration{0, 100 * time.Microsecond, 500 * time.Microsecond}
	for _, d := range delays {
		if d > 0 {
			time.Sleep(d)
		}
		pid := s.scanUDP(srcIP, srcPort)
		if pid != 0 {
			info, _ := s.InspectPID(pid)
			s.cache.Put(key, pid, info.Exe, info.UserSID, 5*time.Second, time.Now())
			return pid, info.Exe, info.UserSID, true
		}
	}
	return 0, "", "", false
}

func (s *SystemLookup) scanUDP(srcIP netip.Addr, srcPort uint16) uint32 {
	if srcIP.Is4() {
		raw, err := s.getTable(procGetExtendedUdpTable, afINET, udpTableOwnerPID)
		if err != nil || len(raw) < 4 {
			return 0
		}
		count := *(*uint32)(unsafe.Pointer(&raw[0]))
		rowSize := unsafe.Sizeof(mibUDPRowOwnerPID{})
		if uintptr(len(raw)) < 4+uintptr(count)*rowSize {
			return 0
		}
		var wildcard uint32
		for i := uint32(0); i < count; i++ {
			row := (*mibUDPRowOwnerPID)(unsafe.Pointer(&raw[4+uintptr(i)*rowSize]))
			if ntohsPort(row.LocalPort) != srcPort {
				continue
			}
			lAddr := netip.AddrFrom4(row.LocalAddr)
			if lAddr == srcIP {
				return row.OwningPID
			}
			if lAddr.IsUnspecified() && wildcard == 0 {
				wildcard = row.OwningPID
			}
		}
		return wildcard
	}

	raw, err := s.getTable(procGetExtendedUdpTable, afINET6, udpTableOwnerPID)
	if err != nil || len(raw) < 4 {
		return 0
	}
	count := *(*uint32)(unsafe.Pointer(&raw[0]))
	rowSize := unsafe.Sizeof(mibUDP6RowOwnerPID{})
	if uintptr(len(raw)) < 4+uintptr(count)*rowSize {
		return 0
	}
	var wildcard uint32
	for i := uint32(0); i < count; i++ {
		row := (*mibUDP6RowOwnerPID)(unsafe.Pointer(&raw[4+uintptr(i)*rowSize]))
		if ntohsPort(row.LocalPort) != srcPort {
			continue
		}
		lAddr := netip.AddrFrom16(row.LocalAddr)
		if lAddr == srcIP {
			return row.OwningPID
		}
		if lAddr.IsUnspecified() && wildcard == 0 {
			wildcard = row.OwningPID
		}
	}
	return wildcard
}

// InspectPID reads executable path and token user SID for pid.
func (s *SystemLookup) InspectPID(pid uint32) (ProcInfo, bool) {
	info := ProcInfo{PID: pid}
	if pid == 0 {
		return info, false
	}
	h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
	if err != nil {
		return info, false
	}
	defer windows.CloseHandle(h)

	buf := make([]uint16, 1024)
	size := uint32(len(buf))
	if err := windows.QueryFullProcessImageName(h, 0, &buf[0], &size); err == nil {
		info.Exe = NormalizeExe(windows.UTF16ToString(buf[:size]))
	}

	var tok windows.Token
	if err := windows.OpenProcessToken(h, windows.TOKEN_QUERY, &tok); err == nil {
		defer tok.Close()
		if tu, err := tok.GetTokenUser(); err == nil && tu != nil && tu.User.Sid != nil {
			info.UserSID = tu.User.Sid.String()
		}
	}
	return info, true
}

// SnapshotProcs captures all running Windows processes with their PPIDs, exes, and user SIDs.
func (s *SystemLookup) SnapshotProcs() []ProcInfo {
	snap, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPPROCESS, 0)
	if err != nil {
		return nil
	}
	defer windows.CloseHandle(snap)

	var pe windows.ProcessEntry32
	pe.Size = uint32(unsafe.Sizeof(pe))
	if err := windows.Process32First(snap, &pe); err != nil {
		return nil
	}
	out := make([]ProcInfo, 0, 256)
	for {
		p := ProcInfo{
			PID:  pe.ProcessID,
			PPID: pe.ParentProcessID,
		}
		if details, ok := s.InspectPID(pe.ProcessID); ok {
			p.Exe = details.Exe
			p.UserSID = details.UserSID
		}
		out = append(out, p)
		if err := windows.Process32Next(snap, &pe); err != nil {
			break
		}
	}
	return out
}

// LivePIDs returns the subset of pids that currently exist.
func (s *SystemLookup) LivePIDs(pids []uint32) []uint32 {
	live := make([]uint32, 0, len(pids))
	for _, pid := range pids {
		h, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
		if err != nil {
			continue
		}
		var code uint32
		err = windows.GetExitCodeProcess(h, &code)
		windows.CloseHandle(h)
		if err == nil && code == 259 { // STILL_ACTIVE
			live = append(live, pid)
		}
	}
	return live
}

// TerminatePIDs stops processes belonging strictly to expectedSID.
func (s *SystemLookup) TerminatePIDs(pids []uint32, expectedSID string, force bool) ([]uint32, []uint32) {
	var stopped, still []uint32
	for _, pid := range pids {
		info, ok := s.InspectPID(pid)
		if !ok {
			continue
		}
		if expectedSID != "" && info.UserSID != expectedSID {
			still = append(still, pid)
			continue
		}
		h, err := windows.OpenProcess(windows.PROCESS_TERMINATE|windows.PROCESS_QUERY_LIMITED_INFORMATION, false, pid)
		if err != nil {
			still = append(still, pid)
			continue
		}
		err = windows.TerminateProcess(h, 1)
		windows.CloseHandle(h)
		if err == nil {
			stopped = append(stopped, pid)
		} else if force {
			still = append(still, pid)
		} else {
			still = append(still, pid)
		}
	}
	return stopped, still
}
