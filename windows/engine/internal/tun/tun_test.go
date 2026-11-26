package tun

import (
	"os"
	"path/filepath"
	"testing"
)

func TestWintunHashMismatchRefused(t *testing.T) {
	tmp := t.TempDir()
	fakeDLL := filepath.Join(tmp, "wintun.dll")
	if err := os.WriteFile(fakeDLL, []byte("not-a-valid-wintun-dll"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := VerifyWintunSHA256(fakeDLL, "amd64"); err == nil {
		t.Fatal("expected VerifyWintunSHA256 to reject tampered wintun.dll")
	}
	if err := VerifyWintunSHA256(filepath.Join(tmp, "missing.dll"), "amd64"); err == nil {
		t.Fatal("expected VerifyWintunSHA256 to fail on missing wintun.dll")
	}
}
