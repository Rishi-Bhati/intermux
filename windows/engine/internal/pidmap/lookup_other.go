//go:build !windows

package pidmap

import "net/netip"

// SystemLookup is a stub outside Windows for cross-platform compilation and unit testing.
type SystemLookup struct{}

func NewSystemLookup() *SystemLookup { return &SystemLookup{} }

func (s *SystemLookup) LookupTCP(srcIP netip.Addr, srcPort uint16, dstIP netip.Addr, dstPort uint16) (uint32, string, string, bool) {
	return 0, "", "", false
}

func (s *SystemLookup) LookupUDP(srcIP netip.Addr, srcPort uint16) (uint32, string, string, bool) {
	return 0, "", "", false
}

func (s *SystemLookup) InspectPID(pid uint32) (ProcInfo, bool) {
	return ProcInfo{}, false
}

func (s *SystemLookup) SnapshotProcs() []ProcInfo { return nil }

func (s *SystemLookup) LivePIDs(pids []uint32) []uint32 { return nil }

func (s *SystemLookup) TerminatePIDs(pids []uint32, expectedSID string, force bool) ([]uint32, []uint32) {
	return nil, nil
}
