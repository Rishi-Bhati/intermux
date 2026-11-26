//go:build !windows

package ipc

import (
	"errors"
	"os"
	"path/filepath"
)

func verifyAdminOnlyInstallPlatform(exePath string) error {
	return nil
}

func currentUserSIDPlatform() (string, error) {
	return "S-1-5-21-0-0-0-1000", nil
}

func writeProtectedTokenFilePlatform(path string, token []byte, userSID string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	return os.WriteFile(path, token, 0o600)
}

func installPasswordlessTaskPlatform(exePath, callerSID string) error {
	return errors.New("Scheduled Tasks are only supported on Windows")
}

func removePasswordlessTaskPlatform(callerSID string) error {
	return errors.New("Scheduled Tasks are only supported on Windows")
}
