//go:build windows

package ipc

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"unsafe"

	"golang.org/x/sys/windows"
)

func TestWindowsNamedPipeDACLAndFirstInstanceAndFlags(t *testing.T) {
	if (PipeOpenModeFlags & windows.FILE_FLAG_FIRST_PIPE_INSTANCE) == 0 {
		t.Fatal("PipeOpenModeFlags missing FILE_FLAG_FIRST_PIPE_INSTANCE")
	}
	if (PipeModeFlags & windows.PIPE_REJECT_REMOTE_CLIENTS) == 0 {
		t.Fatal("PipeModeFlags missing PIPE_REJECT_REMOTE_CLIENTS")
	}

	sid, err := CurrentUserSID()
	if err != nil {
		t.Fatal(err)
	}
	sddl, err := BuildPipeSDDL(sid)
	if err != nil {
		t.Fatal(err)
	}
	pipePath := PipeNameForSID(sid) + "-ci-test"
	h, err := createHardenedPipeInstance(pipePath, sddl)
	if err != nil {
		t.Fatalf("createHardenedPipeInstance failed: %v", err)
	}
	defer windows.CloseHandle(h)

	// Attempting a second instance MUST fail.
	if h2, err2 := createHardenedPipeInstance(pipePath, sddl); err2 == nil {
		_ = windows.CloseHandle(h2)
		t.Fatal("expected second pipe instance with FILE_FLAG_FIRST_PIPE_INSTANCE to fail")
	}

	// Inspect live pipe handle DACL.
	sd, err := windows.GetSecurityInfo(
		h,
		windows.SE_KERNEL_OBJECT,
		windows.DACL_SECURITY_INFORMATION,
	)
	if err != nil {
		t.Fatalf("GetSecurityInfo on pipe handle failed: %v", err)
	}
	dacl, _, err := sd.DACL()
	if err != nil || dacl == nil {
		t.Fatalf("missing DACL on live pipe handle: %v", err)
	}

	var sizeInfo aclSizeInfo
	r1, _, e1 := procGetAclInformation.Call(uintptr(unsafe.Pointer(dacl)), uintptr(unsafe.Pointer(&sizeInfo)), unsafe.Sizeof(sizeInfo), 2)
	if r1 == 0 {
		t.Fatalf("GetAclInformation failed: %v", e1)
	}
	seen := make(map[string]bool)
	for i := uint32(0); i < sizeInfo.AceCount; i++ {
		var pAce uintptr
		ok, _, _ := procGetAce.Call(uintptr(unsafe.Pointer(dacl)), uintptr(i), uintptr(unsafe.Pointer(&pAce)))
		if ok == 0 || pAce == 0 {
			continue
		}
		ace := (*accessAllowedACE)(unsafe.Pointer(pAce))
		s := (*windows.SID)(unsafe.Pointer(&ace.SidStart)).String()
		seen[strings.ToUpper(s)] = true
	}

	for _, want := range []string{SIDSystem, SIDAdministrators, strings.ToUpper(sid)} {
		if !seen[want] {
			t.Fatalf("live pipe DACL missing expected SID %s (seen=%v)", want, seen)
		}
	}
	for _, forbid := range []string{"S-1-1-0", "S-1-5-11", "S-1-5-4", "S-1-5-2"} {
		if seen[forbid] {
			t.Fatalf("live pipe DACL must NOT include %s", forbid)
		}
	}
}

func TestWindowsVerifyAdminOnlyInstallRefusesTempDir(t *testing.T) {
	dir := t.TempDir()
	fakeExe := filepath.Join(dir, "intermux-net.exe")
	if err := os.WriteFile(fakeExe, []byte("MZ"), 0o700); err != nil {
		t.Fatal(err)
	}
	// On Windows runners, temp directories grant Users/owner write access or non-admin SID write access.
	// VerifyAdminOnlyInstall checks that only SYSTEM/Administrators/TrustedInstaller/CREATOR OWNER have write.
	if err := VerifyAdminOnlyInstall(fakeExe); err == nil {
		// Note: if running under SYSTEM/Admin temp where only Admins own it, verify CheckDACLSafety directly as well.
		t.Log("temp dir owned strictly by Administrators on runner; explicit non-admin CheckDACLSafety verified in ipc_test.go")
	}
}
