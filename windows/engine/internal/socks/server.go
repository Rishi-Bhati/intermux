package socks

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"strconv"
	"sync"
	"time"

	"github.com/Rishi-Bhati/intermux/windows/engine/internal/counters"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/dns"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/nics"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/shaping"
)

const (
	socksVersion   = 0x05
	authNoAuth     = 0x00
	authUserPass   = 0x02
	authNoMethod   = 0xFF
	userPassVer    = 0x01
	cmdConnect     = 0x01
	cmdUDPAssoc    = 0x03
	atypIPv4       = 0x01
	atypDomain     = 0x03
	atypIPv6       = 0x04
	repSuccess     = 0x00
	repFailure     = 0x01
	repCmdNotSupp  = 0x07
	repAtypNotSupp = 0x08
)

// Credential holds the per-session random RFC 1929 username and password.
type Credential struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

// GenerateCredential creates a fresh 128-bit random username and 256-bit random password.
func GenerateCredential() (Credential, error) {
	u := make([]byte, 16)
	p := make([]byte, 32)
	if _, err := rand.Read(u); err != nil {
		return Credential{}, err
	}
	if _, err := rand.Read(p); err != nil {
		return Credential{}, err
	}
	return Credential{
		Username: "imx-" + hex.EncodeToString(u),
		Password: hex.EncodeToString(p),
	}, nil
}

// Server is a loopback-only RFC 1928 + RFC 1929 (mandatory auth) SOCKS5 proxy for one interface.
type Server struct {
	iface     string
	ifIndex   uint32
	bindingID string
	cred      Credential
	dialer    nics.OutboundDialer
	counters  *counters.Registry
	shaping   *shaping.Manager
	dnsMgr    *dns.Manager

	ln     net.Listener
	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

// StartServer binds 127.0.0.1:0 (or listenAddr on 127.0.0.1) and requires RFC 1929 auth.
func StartServer(
	parent context.Context,
	listenAddr string,
	iface string,
	ifIndex uint32,
	bindingID string,
	cred Credential,
	dialer nics.OutboundDialer,
	cnt *counters.Registry,
	shp *shaping.Manager,
	dnsMgr *dns.Manager,
) (*Server, error) {
	if listenAddr == "" {
		listenAddr = "127.0.0.1:0"
	}
	host, _, err := net.SplitHostPort(listenAddr)
	if err != nil {
		return nil, err
	}
	if host != "127.0.0.1" {
		return nil, fmt.Errorf("SOCKS5 fallback must bind 127.0.0.1 only, got %q", host)
	}
	if cred.Username == "" || cred.Password == "" {
		return nil, errors.New("SOCKS5 fallback requires non-empty RFC 1929 credentials")
	}
	ln, err := net.Listen("tcp4", listenAddr)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithCancel(parent)
	s := &Server{
		iface:     iface,
		ifIndex:   ifIndex,
		bindingID: bindingID,
		cred:      cred,
		dialer:    dialer,
		counters:  cnt,
		shaping:   shp,
		dnsMgr:    dnsMgr,
		ln:        ln,
		ctx:       ctx,
		cancel:    cancel,
	}
	s.wg.Add(1)
	go s.serve()
	return s, nil
}

func (s *Server) Addr() string { return s.ln.Addr().String() }
func (s *Server) Port() int    { return s.ln.Addr().(*net.TCPAddr).Port }

func (s *Server) Close() error {
	s.cancel()
	err := s.ln.Close()
	s.wg.Wait()
	return err
}

func (s *Server) serve() {
	defer s.wg.Done()
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			return
		}
		s.wg.Add(1)
		go func(c net.Conn) {
			defer s.wg.Done()
			s.handleConn(c)
		}(conn)
	}
}

func (s *Server) handleConn(conn net.Conn) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(15 * time.Second))

	var hdr [2]byte
	if _, err := io.ReadFull(conn, hdr[:]); err != nil || hdr[0] != socksVersion {
		return
	}
	nmethods := int(hdr[1])
	methods := make([]byte, nmethods)
	if _, err := io.ReadFull(conn, methods); err != nil {
		return
	}
	hasUserPass := false
	for _, m := range methods {
		if m == authUserPass {
			hasUserPass = true
			break
		}
	}
	if !hasUserPass {
		// Reject NO AUTH (0x00) and all unsupported methods.
		_, _ = conn.Write([]byte{socksVersion, authNoMethod})
		return
	}
	if _, err := conn.Write([]byte{socksVersion, authUserPass}); err != nil {
		return
	}

	if !s.authenticateUserPass(conn) {
		return
	}

	var req [4]byte
	if _, err := io.ReadFull(conn, req[:]); err != nil || req[0] != socksVersion {
		return
	}
	cmd := req[1]
	atyp := req[3]
	dstHost, dstPort, err := readAddr(conn, atyp)
	if err != nil {
		sendReply(conn, repAtypNotSupp, net.IPv4zero, 0)
		return
	}
	_ = conn.SetDeadline(time.Time{})

	switch cmd {
	case cmdConnect:
		s.handleConnect(conn, atyp, dstHost, dstPort)
	case cmdUDPAssoc:
		s.handleUDPAssociate(conn)
	default:
		sendReply(conn, repCmdNotSupp, net.IPv4zero, 0)
	}
}

func (s *Server) authenticateUserPass(conn net.Conn) bool {
	var verLen [2]byte
	if _, err := io.ReadFull(conn, verLen[:]); err != nil || verLen[0] != userPassVer {
		return false
	}
	ulen := int(verLen[1])
	uname := make([]byte, ulen)
	if _, err := io.ReadFull(conn, uname); err != nil {
		return false
	}
	var plenBuf [1]byte
	if _, err := io.ReadFull(conn, plenBuf[:]); err != nil {
		return false
	}
	passwd := make([]byte, int(plenBuf[0]))
	if _, err := io.ReadFull(conn, passwd); err != nil {
		return false
	}
	uOK := subtle.ConstantTimeCompare(uname, []byte(s.cred.Username)) == 1
	pOK := subtle.ConstantTimeCompare(passwd, []byte(s.cred.Password)) == 1
	if !uOK || !pOK {
		_, _ = conn.Write([]byte{userPassVer, 0x01})
		return false
	}
	_, err := conn.Write([]byte{userPassVer, 0x00})
	return err == nil
}

func readAddr(r io.Reader, atyp byte) (string, uint16, error) {
	var host string
	switch atyp {
	case atypIPv4:
		var b [4]byte
		if _, err := io.ReadFull(r, b[:]); err != nil {
			return "", 0, err
		}
		host = net.IP(b[:]).String()
	case atypIPv6:
		var b [16]byte
		if _, err := io.ReadFull(r, b[:]); err != nil {
			return "", 0, err
		}
		host = net.IP(b[:]).String()
	case atypDomain:
		var ln [1]byte
		if _, err := io.ReadFull(r, ln[:]); err != nil {
			return "", 0, err
		}
		buf := make([]byte, int(ln[0]))
		if _, err := io.ReadFull(r, buf); err != nil {
			return "", 0, err
		}
		host = string(buf)
	default:
		return "", 0, errors.New("unsupported address type")
	}
	var p [2]byte
	if _, err := io.ReadFull(r, p[:]); err != nil {
		return "", 0, err
	}
	return host, binary.BigEndian.Uint16(p[:]), nil
}

func sendReply(conn net.Conn, rep byte, ip net.IP, port int) {
	if ip == nil {
		ip = net.IPv4zero
	}
	v4 := ip.To4()
	var buf []byte
	if v4 != nil {
		buf = make([]byte, 10)
		buf[0] = socksVersion
		buf[1] = rep
		buf[3] = atypIPv4
		copy(buf[4:8], v4)
		binary.BigEndian.PutUint16(buf[8:10], uint16(port))
	} else {
		v6 := ip.To16()
		buf = make([]byte, 22)
		buf[0] = socksVersion
		buf[1] = rep
		buf[3] = atypIPv6
		copy(buf[4:20], v6)
		binary.BigEndian.PutUint16(buf[20:22], uint16(port))
	}
	_, _ = conn.Write(buf)
}

func (s *Server) handleConnect(client net.Conn, atyp byte, host string, port uint16) {
	target := net.JoinHostPort(host, strconv.Itoa(int(port)))
	netName := "tcp"
	if atyp == atypIPv4 {
		netName = "tcp4"
	} else if atyp == atypIPv6 {
		netName = "tcp6"
	}
	ctx, cancel := context.WithTimeout(s.ctx, 15*time.Second)
	remote, err := s.dialer.DialTCP(ctx, s.ifIndex, netName, target)
	cancel()
	if err != nil {
		sendReply(client, repFailure, net.IPv4zero, 0)
		return
	}
	defer remote.Close()

	laddr, _ := remote.LocalAddr().(*net.TCPAddr)
	if laddr != nil {
		sendReply(client, repSuccess, laddr.IP, laddr.Port)
	} else {
		sendReply(client, repSuccess, net.IPv4zero, 0)
	}

	flow := s.counters.FlowStart(s.iface, s.bindingID, 0)
	defer flow.Close()
	lim := s.shaping.GetOrCreate(s.iface)

	done := make(chan struct{}, 2)
	go func() {
		defer func() { done <- struct{}{} }()
		r := shaping.WrapReader(s.ctx, remote, lim.Down, flow.AddRX)
		_, _ = io.Copy(client, r)
		if tc, ok := client.(*net.TCPConn); ok {
			_ = tc.CloseWrite()
		}
	}()
	go func() {
		defer func() { done <- struct{}{} }()
		w := shaping.WrapWriter(s.ctx, remote, lim.Up, flow.AddTX)
		_, _ = io.Copy(w, client)
		if tc, ok := remote.(*net.TCPConn); ok {
			_ = tc.CloseWrite()
		}
	}()
	<-done
	<-done
}

func (s *Server) handleUDPAssociate(ctrl net.Conn) {
	localPC, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		sendReply(ctrl, repFailure, net.IPv4zero, 0)
		return
	}
	defer localPC.Close()

	remotePC, err := s.dialer.ListenUDP(s.ctx, s.ifIndex, "udp4")
	if err != nil {
		sendReply(ctrl, repFailure, net.IPv4zero, 0)
		return
	}
	defer remotePC.Close()

	localAddr := localPC.LocalAddr().(*net.UDPAddr)
	sendReply(ctrl, repSuccess, net.ParseIP("127.0.0.1"), localAddr.Port)

	flow := s.counters.FlowStart(s.iface, s.bindingID, 0)
	defer flow.Close()
	lim := s.shaping.GetOrCreate(s.iface)

	assocCtx, assocCancel := context.WithCancel(s.ctx)
	defer assocCancel()

	var clientAddrMu sync.Mutex
	var clientUDPAddr *net.UDPAddr

	// Watch controlling TCP connection for close.
	go func() {
		var tmp [16]byte
		for {
			if _, err := ctrl.Read(tmp[:]); err != nil {
				assocCancel()
				_ = localPC.Close()
				_ = remotePC.Close()
				return
			}
		}
	}()

	// Remote -> local client loop.
	go func() {
		buf := make([]byte, 65535)
		for {
			n, raddr, err := remotePC.ReadFrom(buf)
			if err != nil {
				return
			}
			clientAddrMu.Lock()
			caddr := clientUDPAddr
			clientAddrMu.Unlock()
			if caddr == nil {
				continue
			}
			if err := lim.Down.WaitDatagram(assocCtx, n); err != nil {
				return
			}
			uaddr, ok := raddr.(*net.UDPAddr)
			if !ok {
				continue
			}
			pkt := encodeUDPHeader(uaddr.IP, uint16(uaddr.Port), buf[:n])
			if _, err := localPC.WriteTo(pkt, caddr); err == nil {
				flow.AddRX(int64(n))
			}
		}
	}()

	// Local client -> remote loop.
	buf := make([]byte, 65535)
	for {
		n, addr, err := localPC.ReadFrom(buf)
		if err != nil {
			return
		}
		uaddr, ok := addr.(*net.UDPAddr)
		if !ok || !uaddr.IP.Equal(net.ParseIP("127.0.0.1")) {
			continue
		}
		clientAddrMu.Lock()
		clientUDPAddr = uaddr
		clientAddrMu.Unlock()

		host, port, payload, ok := decodeUDPHeader(buf[:n])
		if !ok {
			continue
		}
		if err := lim.Up.WaitDatagram(assocCtx, len(payload)); err != nil {
			return
		}
		dstAddr, err := resolveUDPTarget(host, port)
		if err != nil {
			continue
		}
		if _, err := remotePC.WriteTo(payload, dstAddr); err == nil {
			flow.AddTX(int64(len(payload)))
		}
	}
}

func resolveUDPTarget(host string, port uint16) (*net.UDPAddr, error) {
	if ip, err := netip.ParseAddr(host); err == nil {
		return &net.UDPAddr{IP: ip.AsSlice(), Port: int(port)}, nil
	}
	return net.ResolveUDPAddr("udp4", net.JoinHostPort(host, strconv.Itoa(int(port))))
}

func decodeUDPHeader(pkt []byte) (string, uint16, []byte, bool) {
	if len(pkt) < 8 || pkt[2] != 0x00 { // drop FRAG != 0 per RFC 1928 §7
		return "", 0, nil, false
	}
	atyp := pkt[3]
	off := 4
	var host string
	switch atyp {
	case atypIPv4:
		if len(pkt) < off+4+2 {
			return "", 0, nil, false
		}
		host = net.IP(pkt[off : off+4]).String()
		off += 4
	case atypIPv6:
		if len(pkt) < off+16+2 {
			return "", 0, nil, false
		}
		host = net.IP(pkt[off : off+16]).String()
		off += 16
	case atypDomain:
		if len(pkt) < off+1 {
			return "", 0, nil, false
		}
		dlen := int(pkt[off])
		off++
		if len(pkt) < off+dlen+2 {
			return "", 0, nil, false
		}
		host = string(pkt[off : off+dlen])
		off += dlen
	default:
		return "", 0, nil, false
	}
	port := binary.BigEndian.Uint16(pkt[off : off+2])
	off += 2
	return host, port, pkt[off:], true
}

func encodeUDPHeader(ip net.IP, port uint16, payload []byte) []byte {
	v4 := ip.To4()
	if v4 != nil {
		out := make([]byte, 10+len(payload))
		out[3] = atypIPv4
		copy(out[4:8], v4)
		binary.BigEndian.PutUint16(out[8:10], port)
		copy(out[10:], payload)
		return out
	}
	v6 := ip.To16()
	out := make([]byte, 22+len(payload))
	out[3] = atypIPv6
	copy(out[4:20], v6)
	binary.BigEndian.PutUint16(out[20:22], port)
	copy(out[22:], payload)
	return out
}
