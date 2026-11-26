package ipc

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

var sidPattern = regexp.MustCompile(`^S-1-(?:0|1|2|3|5|16)(?:-\d+)+$`)

const (
	// Well-known admin/system SIDs allowed to hold write permissions on %ProgramFiles%\InterMux.
	SIDSystem           = "S-1-5-18"
	SIDAdministrators   = "S-1-5-32-544"
	SIDTrustedInstaller = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
	SIDCreatorOwner     = "S-1-3-0"

	// Win32 access mask bits representing write/modify/ownership powers on a file or directory.
	WriteAccessMask uint32 = 0x0002 | // FILE_WRITE_DATA / FILE_ADD_FILE
		0x0004 | // FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
		0x0010 | // FILE_WRITE_EA
		0x0100 | // FILE_WRITE_ATTRIBUTES
		0x00010000 | // DELETE
		0x00040000 | // WRITE_DAC
		0x00080000 | // WRITE_OWNER
		0x10000000 | // GENERIC_ALL
		0x40000000 // GENERIC_WRITE
)

// ACLEntry represents an Access Control Entry inspected from a file or directory DACL.
type ACLEntry struct {
	SID        string
	AccessMask uint32
	IsAllow    bool
}

// ValidateSID checks basic Windows SID syntax (e.g. S-1-5-21-...).
func ValidateSID(sid string) (string, error) {
	s := strings.TrimSpace(sid)
	if !sidPattern.MatchString(s) {
		return "", fmt.Errorf("invalid Windows SID: %q", sid)
	}
	return s, nil
}

// ShortSID returns a deterministic 12-hex suffix derived from sid.
func ShortSID(sid string) string {
	sum := sha256.Sum256([]byte(strings.ToUpper(strings.TrimSpace(sid))))
	return hex.EncodeToString(sum[:6])
}

// PipeNameForSID returns the user-scoped Named Pipe path for sid.
func PipeNameForSID(sid string) string {
	return `\\.\pipe\intermux-net-` + ShortSID(sid)
}

// TaskNameForSID returns the Scheduled Task path for passwordless mode.
func TaskNameForSID(sid string) string {
	return `\InterMux\Helper-` + ShortSID(sid)
}

// BuildPipeSDDL constructs a Protected DACL granting Generic All only to SYSTEM, Administrators, and callerSID.
// Strictly excludes Everyone (WD), Authenticated Users (AU), Interactive (IU), and Network (NU).
func BuildPipeSDDL(callerSID string) (string, error) {
	sid, err := ValidateSID(callerSID)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;%s)", sid), nil
}

// BuildTokenFileSDDL constructs a Protected DACL granting Full Access to SYSTEM, Administrators, and userSID.
func BuildTokenFileSDDL(userSID string) (string, error) {
	sid, err := ValidateSID(userSID)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;%s)", sid), nil
}

// CheckDACLSafety verifies that no non-admin SID holds write/modify/full-control permissions.
func CheckDACLSafety(target string, entries []ACLEntry) error {
	if len(entries) == 0 {
		return fmt.Errorf("security check failed for %s: empty DACL", target)
	}
	for _, e := range entries {
		if !e.IsAllow {
			continue
		}
		if (e.AccessMask & WriteAccessMask) == 0 {
			continue
		}
		switch strings.ToUpper(e.SID) {
		case SIDSystem, SIDAdministrators, SIDTrustedInstaller, SIDCreatorOwner:
			continue
		default:
			return fmt.Errorf("security check failed: %s is writable by non-admin SID %s (mask=0x%x)", target, e.SID, e.AccessMask)
		}
	}
	return nil
}

// BuildScheduledTaskXML produces the exact Scheduled Task XML for passwordless helper launch.
// Only the fixed Program Files exePath and fixed --allowed-sid argument are permitted.
func BuildScheduledTaskXML(exePath string, callerSID string) (string, error) {
	sid, err := ValidateSID(callerSID)
	if err != nil {
		return "", err
	}
	if !strings.HasSuffix(strings.ToLower(exePath), `\intermux-net.exe`) {
		return "", errors.New("scheduled task executable must be intermux-net.exe")
	}
	esc := func(s string) string {
		r := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;", "'", "&apos;")
		return r.Replace(s)
	}
	return fmt.Sprintf(`<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals>
    <Principal id="Author">
      <UserId>%s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%s</Command>
      <Arguments>daemon --allowed-sid %s</Arguments>
    </Exec>
  </Actions>
</Task>
`, esc(sid), esc(exePath), esc(sid)), nil
}

// VerifyAdminOnlyInstall checks that exePath and its parent directory are not writable by non-admins.
func VerifyAdminOnlyInstall(exePath string) error {
	return verifyAdminOnlyInstallPlatform(exePath)
}

// CurrentUserSID returns the current process token user SID on Windows (or empty on non-Windows).
func CurrentUserSID() (string, error) {
	return currentUserSIDPlatform()
}

// WriteProtectedTokenFile writes token bytes to path with a user-only protected ACL.
func WriteProtectedTokenFile(path string, token []byte, userSID string) error {
	return writeProtectedTokenFilePlatform(path, token, userSID)
}

// InstallPasswordlessTask creates the per-user highest-privilege Scheduled Task pointing to exePath.
func InstallPasswordlessTask(exePath, callerSID string) error {
	return installPasswordlessTaskPlatform(exePath, callerSID)
}

// RemovePasswordlessTask deletes the per-user Scheduled Task.
func RemovePasswordlessTask(callerSID string) error {
	return removePasswordlessTaskPlatform(callerSID)
}
