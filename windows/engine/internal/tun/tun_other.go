//go:build !windows

package tun

import "errors"

func initDLLSearchPolicyPlatform() error { return nil }

func verifyWintunAuthenticodePlatform(dllPath string) error { return nil }

func openPlatform(cfg Config) (Device, error) {
	return nil, errors.New("wintun is only supported on Windows")
}

func sweepStalePlatform(adapterName string) error { return nil }
