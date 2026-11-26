package tun

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/netip"
	"os"
	"runtime"
	"strings"
)

const (
	DefaultAdapterName = "InterMux"
	DefaultTunnelType  = "InterMux"
	DefaultMTU         = 9000
	DefaultRingCap     = 0x400000 // 4 MiB
)

// Pinned SHA-256 checksums for official WireGuard Wintun 0.14.1 DLLs.
var PinnedWintunSHA256 = map[string]string{
	"amd64": "e5da8447dc2c320edc0fc52fa01885c103de8c118481f683643cacc3220dafce",
	"arm64": "f7ba89005544be9d85231a9e0d5f23b2d15b3311667e2dad0debd344918a3f80",
	"386":   "d694fa46ab4cfebcb2632d094c7aa97278eef2f8052438621766d863ae98a931",
}

// Config specifies Wintun adapter creation options.
type Config struct {
	AdapterName string
	MTU         uint32
	RingCap     uint32
	ExcludeV4   []netip.Prefix
	ExcludeV6   []netip.Prefix
	EnableIPv6  bool
}

// Device represents an active L3 virtual TUN adapter.
type Device interface {
	io.ReadWriteCloser
	Name() string
	MTU() uint32
	LUID() uint64
	IfIndex() uint32
	AddrV4() netip.Prefix
	AddrV6() netip.Prefix
	CleanupRoutes() error
}

// ExpectedWintunHash returns the compile-time pinned SHA-256 digest for arch (defaults to runtime.GOARCH).
func ExpectedWintunHash(arch string) (string, error) {
	if arch == "" {
		arch = runtime.GOARCH
	}
	want, ok := PinnedWintunSHA256[strings.ToLower(arch)]
	if !ok {
		return "", fmt.Errorf("unsupported architecture for pinned wintun.dll: %s", arch)
	}
	return want, nil
}

// VerifyWintunSHA256 reads dllPath and verifies its SHA-256 matches the pinned digest for arch.
func VerifyWintunSHA256(dllPath string, arch string) error {
	want, err := ExpectedWintunHash(arch)
	if err != nil {
		return err
	}
	f, err := os.Open(dllPath)
	if err != nil {
		return fmt.Errorf("cannot open wintun.dll at %s: %w", dllPath, err)
	}
	defer f.Close()

	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return fmt.Errorf("cannot hash wintun.dll: %w", err)
	}
	got := hex.EncodeToString(h.Sum(nil))
	if !strings.EqualFold(got, want) {
		return fmt.Errorf("wintun.dll SHA-256 mismatch for %s: expected %s, got %s", arch, want, got)
	}
	return nil
}

// InitDLLSearchPolicy locks down the Windows DLL search path at process startup.
func InitDLLSearchPolicy() error {
	return initDLLSearchPolicyPlatform()
}

// VerifyWintunDLL checks pinned SHA-256 and Authenticode signature of dllPath.
func VerifyWintunDLL(dllPath string) error {
	if err := VerifyWintunSHA256(dllPath, runtime.GOARCH); err != nil {
		return err
	}
	return verifyWintunAuthenticodePlatform(dllPath)
}

// Open opens/creates the Wintun device, assigns subnet & MTU, and installs failsafe /1 routes.
func Open(cfg Config) (Device, error) {
	return openPlatform(cfg)
}

// SweepStale cleans up leftover InterMux adapters or /1 routes from a previous crash.
func SweepStale(adapterName string) error {
	return sweepStalePlatform(adapterName)
}
