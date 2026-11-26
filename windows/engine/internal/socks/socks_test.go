package socks

import (
	"bytes"
	"context"
	"encoding/binary"
	"io"
	"net"
	"testing"

	"github.com/Rishi-Bhati/intermux/windows/engine/internal/counters"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/dns"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/nics"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/shaping"
)

func socksHandshakeAuth(t *testing.T, addr string, cred Credential) net.Conn {
	t.Helper()
	conn, err := net.Dial("tcp", addr)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := conn.Write([]byte{socksVersion, 1, authUserPass}); err != nil {
		t.Fatal(err)
	}
	var m [2]byte
	if _, err := io.ReadFull(conn, m[:]); err != nil || m != [2]byte{socksVersion, authUserPass} {
		t.Fatalf("unexpected method reply: %v %v", m, err)
	}
	authReq := []byte{userPassVer, byte(len(cred.Username))}
	authReq = append(authReq, cred.Username...)
	authReq = append(authReq, byte(len(cred.Password)))
	authReq = append(authReq, cred.Password...)
	if _, err := conn.Write(authReq); err != nil {
		t.Fatal(err)
	}
	var a [2]byte
	if _, err := io.ReadFull(conn, a[:]); err != nil || a != [2]byte{userPassVer, 0x00} {
		t.Fatalf("auth failed: %v %v", a, err)
	}
	return conn
}

func TestSocks5RejectsNoAuthAndRequiresRandomCreds(t *testing.T) {
	cred, err := GenerateCredential()
	if err != nil {
		t.Fatal(err)
	}
	cnt := counters.NewRegistry()
	shp := shaping.NewManager()
	dialer := nics.NewSystemDialer()
	srv, err := StartServer(context.Background(), "127.0.0.1:0", "Wi-Fi", 1, "b1", cred, dialer, cnt, shp, dns.NewManager(dialer))
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()

	// Unauthenticated METHOD 0x00 must be rejected with 0xFF.
	conn, err := net.Dial("tcp", srv.Addr())
	if err != nil {
		t.Fatal(err)
	}
	_, _ = conn.Write([]byte{socksVersion, 1, authNoAuth})
	var reply [2]byte
	if _, err := io.ReadFull(conn, reply[:]); err != nil || reply[1] != authNoMethod {
		t.Fatalf("expected 0xFF rejection for NO AUTH, got %v err=%v", reply, err)
	}
	conn.Close()

	// Binding non-loopback must fail.
	if _, err := StartServer(context.Background(), "0.0.0.0:0", "Wi-Fi", 1, "b1", cred, dialer, cnt, shp, dns.NewManager(dialer)); err == nil {
		t.Fatal("expected non-127.0.0.1 bind to be rejected")
	}
}

func TestSocks5ConnectAndUDPAssociate(t *testing.T) {
	// Echo TCP server.
	tcpLn, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer tcpLn.Close()
	go func() {
		for {
			c, err := tcpLn.Accept()
			if err != nil {
				return
			}
			go func(cc net.Conn) {
				defer cc.Close()
				_, _ = io.Copy(cc, cc)
			}(c)
		}
	}()

	// Echo UDP server.
	udpEcho, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer udpEcho.Close()
	go func() {
		buf := make([]byte, 2048)
		for {
			n, addr, err := udpEcho.ReadFrom(buf)
			if err != nil {
				return
			}
			_, _ = udpEcho.WriteTo(buf[:n], addr)
		}
	}()

	cred, _ := GenerateCredential()
	cnt := counters.NewRegistry()
	shp := shaping.NewManager()
	dialer := nics.NewSystemDialer()
	srv, err := StartServer(context.Background(), "127.0.0.1:0", "Wi-Fi", 1, "b1", cred, dialer, cnt, shp, dns.NewManager(dialer))
	if err != nil {
		t.Fatal(err)
	}
	defer srv.Close()

	// Test TCP CONNECT.
	c := socksHandshakeAuth(t, srv.Addr(), cred)
	tAddr := tcpLn.Addr().(*net.TCPAddr)
	req := []byte{socksVersion, cmdConnect, 0x00, atypIPv4}
	req = append(req, tAddr.IP.To4()...)
	var p [2]byte
	binary.BigEndian.PutUint16(p[:], uint16(tAddr.Port))
	req = append(req, p[:]...)
	_, _ = c.Write(req)
	var resp [10]byte
	if _, err := io.ReadFull(c, resp[:]); err != nil || resp[1] != repSuccess {
		t.Fatalf("CONNECT failed: %v err=%v", resp, err)
	}
	msg := []byte("hello-intermux-tcp")
	_, _ = c.Write(msg)
	got := make([]byte, len(msg))
	if _, err := io.ReadFull(c, got); err != nil || !bytes.Equal(got, msg) {
		t.Fatalf("TCP echo mismatch: %q err=%v", got, err)
	}
	c.Close()

	// Test UDP ASSOCIATE.
	uc := socksHandshakeAuth(t, srv.Addr(), cred)
	_, _ = uc.Write([]byte{socksVersion, cmdUDPAssoc, 0x00, atypIPv4, 0, 0, 0, 0, 0, 0})
	var uresp [10]byte
	if _, err := io.ReadFull(uc, uresp[:]); err != nil || uresp[1] != repSuccess {
		t.Fatalf("UDP ASSOCIATE failed: %v err=%v", uresp, err)
	}
	relayPort := binary.BigEndian.Uint16(uresp[8:10])
	udpClient, err := net.DialUDP("udp4", nil, &net.UDPAddr{IP: net.ParseIP("127.0.0.1"), Port: int(relayPort)})
	if err != nil {
		t.Fatal(err)
	}
	defer udpClient.Close()
	echoAddr := udpEcho.LocalAddr().(*net.UDPAddr)
	pkt := encodeUDPHeader(echoAddr.IP, uint16(echoAddr.Port), []byte("hello-udp"))
	_, _ = udpClient.Write(pkt)
	rbuf := make([]byte, 1024)
	rn, err := udpClient.Read(rbuf)
	if err != nil {
		t.Fatal(err)
	}
	_, _, rpayload, ok := decodeUDPHeader(rbuf[:rn])
	if !ok || string(rpayload) != "hello-udp" {
		t.Fatalf("UDP echo mismatch: ok=%v payload=%q", ok, rpayload)
	}
	uc.Close()

	snap := cnt.Snapshot()
	if snap.Interfaces["Wi-Fi"].RXBytes == 0 || snap.Interfaces["Wi-Fi"].TXBytes == 0 {
		t.Fatalf("expected non-zero SOCKS5 byte counters, got %+v", snap.Interfaces["Wi-Fi"])
	}
}
