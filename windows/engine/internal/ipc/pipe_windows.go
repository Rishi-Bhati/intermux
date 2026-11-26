//go:build windows

package ipc

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

const (
	PipeOpenModeFlags uint32 = windows.PIPE_ACCESS_DUPLEX | windows.FILE_FLAG_FIRST_PIPE_INSTANCE
	PipeModeFlags     uint32 = windows.PIPE_TYPE_BYTE | windows.PIPE_READMODE_BYTE | windows.PIPE_WAIT | windows.PIPE_REJECT_REMOTE_CLIENTS
)

var (
	kernel32Pipe                  = syscall.NewLazyDLL("kernel32.dll")
	procGetNamedPipeClientPID     = kernel32Pipe.NewProc("GetNamedPipeClientProcessId")
)

func pipeClientSID(pipeHandle windows.Handle) (string, error) {
	var clientPID uint32
	r1, _, e1 := procGetNamedPipeClientPID.Call(uintptr(pipeHandle), uintptr(unsafe.Pointer(&clientPID)))
	if r1 == 0 {
		return "", fmt.Errorf("GetNamedPipeClientProcessId failed: %w", e1)
	}
	hProc, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, clientPID)
	if err != nil {
		return "", fmt.Errorf("OpenProcess(%d) failed: %w", clientPID, err)
	}
	defer windows.CloseHandle(hProc)

	var tok windows.Token
	if err := windows.OpenProcessToken(hProc, windows.TOKEN_QUERY, &tok); err != nil {
		return "", fmt.Errorf("OpenProcessToken(%d) failed: %w", clientPID, err)
	}
	defer tok.Close()
	tu, err := tok.GetTokenUser()
	if err != nil || tu == nil || tu.User.Sid == nil {
		return "", fmt.Errorf("GetTokenUser(%d) failed: %w", clientPID, err)
	}
	return tu.User.Sid.String(), nil
}

func createHardenedPipeInstance(pipePath, sddl string) (windows.Handle, error) {
	sd, err := windows.SecurityDescriptorFromString(sddl)
	if err != nil {
		return windows.InvalidHandle, fmt.Errorf("invalid pipe SDDL: %w", err)
	}
	sa := &windows.SecurityAttributes{
		Length:             uint32(unsafe.Sizeof(windows.SecurityAttributes{})),
		SecurityDescriptor: sd,
	}
	p16, err := windows.UTF16PtrFromString(pipePath)
	if err != nil {
		return windows.InvalidHandle, err
	}
	// First instance MUST succeed exclusively; no fallback pipe name is allowed.
	h, err := windows.CreateNamedPipe(
		p16,
		PipeOpenModeFlags,
		PipeModeFlags,
		1,
		65536,
		65536,
		0,
		sa,
	)
	if err != nil {
		return windows.InvalidHandle, fmt.Errorf("CreateNamedPipe(%s) failed (refusing fallback): %w", pipePath, err)
	}
	return h, nil
}

// ServeNamedPipe serves JSON v1 requests over a hardened single-user Named Pipe.
func ServeNamedPipe(ctx context.Context, pipePath string, allowedSID string, handler *Handler) error {
	sddl, err := BuildPipeSDDL(allowedSID)
	if err != nil {
		return err
	}
	if pipePath == "" {
		pipePath = PipeNameForSID(allowedSID)
	}

	// Verify we hold the exclusive first instance before accepting clients.
	h, err := createHardenedPipeInstance(pipePath, sddl)
	if err != nil {
		return err
	}

	var mu sync.Mutex
	var activeHandle = h
	closeActive := func() {
		mu.Lock()
		ch := activeHandle
		activeHandle = windows.InvalidHandle
		mu.Unlock()
		if ch != windows.InvalidHandle && ch != 0 {
			_ = windows.DisconnectNamedPipe(ch)
			_ = windows.CloseHandle(ch)
		}
	}

	go func() {
		<-ctx.Done()
		closeActive()
	}()

	for {
		if ctx.Err() != nil {
			closeActive()
			return nil
		}
		err := windows.ConnectNamedPipe(h, nil)
		if err != nil && !errors.Is(err, windows.ERROR_PIPE_CONNECTED) {
			_ = windows.CloseHandle(h)
			if ctx.Err() != nil {
				return nil
			}
			return fmt.Errorf("ConnectNamedPipe: %w", err)
		}

		clientSID, sidErr := pipeClientSID(h)
		if sidErr != nil || !strings.EqualFold(clientSID, allowedSID) {
			_ = windows.DisconnectNamedPipe(h)
			_ = windows.CloseHandle(h)
			h, err = createHardenedPipeInstance(pipePath, sddl)
			if err != nil {
				return err
			}
			mu.Lock()
			activeHandle = h
			mu.Unlock()
			continue
		}

		f := os.NewFile(uintptr(h), pipePath)
		servePipeFile(f, clientSID, handler)
		_ = windows.DisconnectNamedPipe(h)
		_ = f.Close() // also closes h

		if ctx.Err() != nil {
			return nil
		}
		h, err = createHardenedPipeInstance(pipePath, sddl)
		if err != nil {
			return err
		}
		mu.Lock()
		activeHandle = h
		mu.Unlock()
	}
}

func servePipeFile(rw io.ReadWriter, clientSID string, handler *Handler) {
	reader := bufio.NewReaderSize(rw, MaxRequestBytes+1024)
	for {
		line, err := reader.ReadBytes('\n')
		if len(line) > 0 {
			resp := handler.Handle(line, clientSID)
			out, _ := json.Marshal(resp)
			out = append(out, '\n')
			_, _ = rw.Write(out)
		}
		if err != nil {
			return
		}
	}
}
