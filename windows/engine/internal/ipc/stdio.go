package ipc

import (
	"bufio"
	"encoding/json"
	"io"
)

// ServeStdio reads one (or streaming) JSON v1 lines from r and writes JSON v1 responses to w.
func ServeStdio(r io.Reader, w io.Writer, callerSID string, handler *Handler, stream bool) bool {
	reader := bufio.NewReaderSize(r, MaxRequestBytes+1024)
	allOK := true
	for {
		line, err := reader.ReadBytes('\n')
		if len(line) > 0 {
			resp := handler.Handle(line, callerSID)
			if !resp.OK {
				allOK = false
			}
			out, _ := json.Marshal(resp)
			_, _ = w.Write(append(out, '\n'))
		}
		if !stream || err != nil {
			break
		}
	}
	return allOK
}
