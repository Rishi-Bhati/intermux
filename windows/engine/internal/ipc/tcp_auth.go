package ipc

import (
	"bufio"
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"strings"
	"time"
)

// LoopbackServer serves JSON v1 over 127.0.0.1 authenticated by a per-session 256-bit nonce.
type LoopbackServer struct {
	ln      net.Listener
	token   string
	userSID string
	handler *Handler
}

// StartLoopback binds 127.0.0.1 only, optionally persists the nonce to tokenFile with protected ACL, and serves requests.
func StartLoopback(ctx context.Context, listenAddr string, tokenFile string, userSID string, handler *Handler) (*LoopbackServer, error) {
	if listenAddr == "" {
		listenAddr = "127.0.0.1:0"
	}
	host, _, err := net.SplitHostPort(listenAddr)
	if err != nil {
		return nil, err
	}
	if host != "127.0.0.1" {
		return nil, fmt.Errorf("loopback IPC must bind 127.0.0.1 only, got %q", host)
	}

	rawTok := make([]byte, 32)
	if _, err := rand.Read(rawTok); err != nil {
		return nil, err
	}
	token := hex.EncodeToString(rawTok)

	if tokenFile != "" {
		if err := WriteProtectedTokenFile(tokenFile, []byte(token+"\n"), userSID); err != nil {
			return nil, err
		}
	}

	ln, err := net.Listen("tcp4", listenAddr)
	if err != nil {
		return nil, err
	}

	srv := &LoopbackServer{
		ln:      ln,
		token:   token,
		userSID: userSID,
		handler: handler,
	}
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	go srv.serve()
	return srv, nil
}

func (s *LoopbackServer) Addr() string  { return s.ln.Addr().String() }
func (s *LoopbackServer) Token() string { return s.token }
func (s *LoopbackServer) Close() error  { return s.ln.Close() }

func (s *LoopbackServer) serve() {
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			return
		}
		go s.handleConn(conn)
	}
}

func (s *LoopbackServer) handleConn(conn net.Conn) {
	defer conn.Close()
	_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
	reader := bufio.NewReaderSize(conn, MaxRequestBytes+1024)

	authLine, err := reader.ReadBytes('\n')
	if err != nil {
		return
	}
	var authObj struct {
		V    int    `json:"v"`
		Auth string `json:"auth"`
	}
	if json.Unmarshal(authLine, &authObj) != nil || authObj.V != ProtocolVersion ||
		subtle.ConstantTimeCompare([]byte(strings.TrimSpace(authObj.Auth)), []byte(s.token)) != 1 {
		out, _ := json.Marshal(ErrorResponse("permission_denied", "invalid or missing auth token", nil))
		_, _ = conn.Write(append(out, '\n'))
		return
	}
	_ = conn.SetReadDeadline(time.Time{})
	okOut, _ := json.Marshal(OkResponse(map[string]bool{"authenticated": true}))
	_, _ = conn.Write(append(okOut, '\n'))

	for {
		line, err := reader.ReadBytes('\n')
		if len(line) > 0 {
			resp := s.handler.Handle(line, s.userSID)
			out, _ := json.Marshal(resp)
			_, _ = conn.Write(append(out, '\n'))
		}
		if err != nil {
			return
		}
	}
}
