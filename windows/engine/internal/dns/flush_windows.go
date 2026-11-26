//go:build windows

package dns

import "syscall"

var (
	dnsapi                  = syscall.NewLazyDLL("dnsapi.dll")
	procDnsFlushResolverCache = dnsapi.NewProc("DnsFlushResolverCache")
)

// FlushSystemCache flushes Windows dnscache (svchost.exe) so new DNS policies take effect immediately.
func FlushSystemCache() {
	_, _, _ = procDnsFlushResolverCache.Call()
}
