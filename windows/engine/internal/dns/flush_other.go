//go:build !windows

package dns

// FlushSystemCache is a no-op outside Windows.
func FlushSystemCache() {}
