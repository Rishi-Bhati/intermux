//go:build !windows

package ipc

import (
	"context"
	"errors"
)

const (
	PipeOpenModeFlags uint32 = 0x00080003
	PipeModeFlags     uint32 = 0x00000008
)

func ServeNamedPipe(ctx context.Context, pipePath string, allowedSID string, handler *Handler) error {
	return errors.New("Windows Named Pipes are only supported on Windows")
}
