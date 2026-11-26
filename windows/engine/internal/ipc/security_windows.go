//go:build windows

package ipc

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unicode/utf16"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	accessAllowedACEType = 0
)

type aceHeader struct {
	AceType  byte
	AceFlags byte
	AceSize  uint16
}

type accessAllowedACE struct {
	Header   aceHeader
	Mask     uint32
	SidStart uint32
}

type aclSizeInfo struct {
	AceCount      uint32
	AclBytesInUse uint32
	AclBytesFree  uint32
}

var (
	advapi32              = syscall.NewLazyDLL("advapi32.dll")
	procGetAclInformation = advapi32.NewProc("GetAclInformation")
	procGetAce            = advapi32.NewProc("GetAce")
)

func readPathDACL(target string) ([]ACLEntry, error) {
	sd, err := windows.GetNamedSecurityInfo(
		target,
		windows.SE_FILE_OBJECT,
		windows.DACL_SECURITY_INFORMATION,
	)
	if err != nil {
		return nil, fmt.Errorf("GetNamedSecurityInfo(%s): %w", target, err)
	}
	dacl, _, err := sd.DACL()
	if err != nil || dacl == nil {
		return nil, fmt.Errorf("missing DACL on %s: %w", target, err)
	}

	var sizeInfo aclSizeInfo
	r1, _, e1 := procGetAclInformation.Call(
		uintptr(unsafe.Pointer(dacl)),
		uintptr(unsafe.Pointer(&sizeInfo)),
		unsafe.Sizeof(sizeInfo),
		2, // AclSizeInformation
	)
	if r1 == 0 {
		return nil, fmt.Errorf("GetAclInformation(%s) failed: %w", target, e1)
	}

	entries := make([]ACLEntry, 0, sizeInfo.AceCount)
	for i := uint32(0); i < sizeInfo.AceCount; i++ {
		var pAce uintptr
		ok, _, _ := procGetAce.Call(uintptr(unsafe.Pointer(dacl)), uintptr(i), uintptr(unsafe.Pointer(&pAce)))
		if ok == 0 || pAce == 0 {
			continue
		}
		hdr := (*aceHeader)(unsafe.Pointer(pAce))
		if hdr.AceType != accessAllowedACEType {
			continue
		}
		ace := (*accessAllowedACE)(unsafe.Pointer(pAce))
		sid := (*windows.SID)(unsafe.Pointer(&ace.SidStart))
		entries = append(entries, ACLEntry{
			SID:        sid.String(),
			AccessMask: ace.Mask,
			IsAllow:    true,
		})
	}
	return entries, nil
}

func verifyAdminOnlyInstallPlatform(exePath string) error {
	absExe, err := filepath.Abs(exePath)
	if err != nil {
		return err
	}
	dir := filepath.Dir(absExe)

	dirEntries, err := readPathDACL(dir)
	if err != nil {
		return err
	}
	if err := CheckDACLSafety(dir, dirEntries); err != nil {
		return err
	}

	exeEntries, err := readPathDACL(absExe)
	if err != nil {
		return err
	}
	return CheckDACLSafety(absExe, exeEntries)
}

func currentUserSIDPlatform() (string, error) {
	var tok windows.Token
	if err := windows.OpenProcessToken(windows.CurrentProcess(), windows.TOKEN_QUERY, &tok); err != nil {
		return "", err
	}
	defer tok.Close()
	tu, err := tok.GetTokenUser()
	if err != nil || tu == nil || tu.User.Sid == nil {
		return "", fmt.Errorf("GetTokenUser failed: %w", err)
	}
	return tu.User.Sid.String(), nil
}

func writeProtectedTokenFilePlatform(path string, token []byte, userSID string) error {
	if userSID == "" {
		var err error
		userSID, err = currentUserSIDPlatform()
		if err != nil {
			return err
		}
	}
	sddl, err := BuildTokenFileSDDL(userSID)
	if err != nil {
		return err
	}
	sd, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return err
	}
	sa := &windows.SecurityAttributes{
		Length:             uint32(unsafe.Sizeof(windows.SecurityAttributes{})),
		SecurityDescriptor: sd,
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	p16, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	h, err := windows.CreateFile(
		p16,
		windows.GENERIC_WRITE,
		0,
		sa,
		windows.CREATE_ALWAYS,
		windows.FILE_ATTRIBUTE_NORMAL,
		0,
	)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(h)
	var written uint32
	return windows.WriteFile(h, token, &written, nil)
}

func installPasswordlessTaskPlatform(exePath, callerSID string) error {
	if err := VerifyAdminOnlyInstall(exePath); err != nil {
		return err
	}
	xmlText, err := BuildScheduledTaskXML(exePath, callerSID)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp("", "intermux-task-*.xml")
	if err != nil {
		return err
	}
	tmpPath := f.Name()
	defer os.Remove(tmpPath)

	// Task Scheduler expects UTF-16LE BOM for XML import.
	u16 := utf16.Encode([]rune(xmlText))
	raw := make([]byte, 2+len(u16)*2)
	raw[0], raw[1] = 0xFF, 0xFE
	for i, c := range u16 {
		raw[2+i*2] = byte(c)
		raw[3+i*2] = byte(c >> 8)
	}
	if _, err := f.Write(raw); err != nil {
		f.Close()
		return err
	}
	f.Close()

	taskName := TaskNameForSID(callerSID)
	cmd := exec.Command("schtasks.exe", "/Create", "/TN", taskName, "/XML", tmpPath, "/F")
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("schtasks /Create failed: %w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}

func removePasswordlessTaskPlatform(callerSID string) error {
	taskName := TaskNameForSID(callerSID)
	cmd := exec.Command("schtasks.exe", "/Delete", "/TN", taskName, "/F")
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("schtasks /Delete failed: %w: %s", err, strings.TrimSpace(string(out)))
	}
	return nil
}
