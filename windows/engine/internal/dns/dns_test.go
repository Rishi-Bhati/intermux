package dns

import (
	"context"
	"encoding/binary"
	"net"
	"testing"
	"time"
)

type mockDialer struct{}

func (mockDialer) DialTCP(ctx context.Context, ifIndex uint32, network, addr string) (net.Conn, error) {
	return (&net.Dialer{}).DialContext(ctx, network, addr)
}

func (mockDialer) ListenUDP(ctx context.Context, ifIndex uint32, network string) (net.PacketConn, error) {
	return net.ListenPacket(network, "127.0.0.1:0")
}

func TestValidateServersAndProbe(t *testing.T) {
	valid, err := ValidateServers([]string{"1.1.1.1", "8.8.8.8", "1.1.1.1"})
	if err != nil || len(valid) != 2 {
		t.Fatalf("unexpected valid servers: %v err=%v", valid, err)
	}
	for _, bad := range []string{"127.0.0.1", "169.254.1.1", "224.0.0.1", "0.0.0.0", "255.255.255.255", "not-an-ip"} {
		if _, err := ValidateServer(bad); err == nil {
			t.Fatalf("expected error for %q", bad)
		}
	}

	q := BuildProbeQuery()
	if len(q) != 17 || binary.BigEndian.Uint16(q[:2]) != ProbeQID {
		t.Fatalf("unexpected probe query: %x", q)
	}

	resp := append([]byte(nil), q...)
	binary.BigEndian.PutUint16(resp[2:4], 0x8180) // QR=1, NOERROR
	if !IsValidProbeReply(resp) {
		t.Fatal("expected valid probe reply")
	}
	binary.BigEndian.PutUint16(resp[2:4], 0x8182) // SERVFAIL
	if IsValidProbeReply(resp) {
		t.Fatal("expected SERVFAIL to be invalid probe reply")
	}
}

func TestForwardUDP(t *testing.T) {
	pc, err := net.ListenPacket("udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer pc.Close()

	// Spin up local stub responding with mirrored QID + QR=1.
	go func() {
		buf := make([]byte, 512)
		n, addr, err := pc.ReadFrom(buf)
		if err != nil || n < 12 {
			return
		}
		reply := make([]byte, n)
		copy(reply, buf[:n])
		binary.BigEndian.PutUint16(reply[2:4], 0x8180)
		_, _ = pc.WriteTo(reply, addr)
	}()

	// Dial directly to loopback stub port using a custom PacketConn wrapper.
	stubAddr := pc.LocalAddr().(*net.UDPAddr)
	mgr := NewManager(&stubPortDialer{port: stubAddr.Port})
	q := BuildProbeQuery()
	reply, err := mgr.ForwardUDP(context.Background(), q, 1, []string{"127.0.0.1"})
	if err != nil {
		t.Fatalf("ForwardUDP failed: %v", err)
	}
	if !IsValidProbeReply(reply) {
		t.Fatalf("unexpected DNS reply: %x", reply)
	}
}

type stubPortDialer struct {
	port int
}

func (s *stubPortDialer) DialTCP(ctx context.Context, ifIndex uint32, network, addr string) (net.Conn, error) {
	return (&net.Dialer{}).DialContext(ctx, network, addr)
}

func (s *stubPortDialer) ListenUDP(ctx context.Context, ifIndex uint32, network string) (net.PacketConn, error) {
	pc, err := net.ListenPacket(network, "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	return &portRedirectPacketConn{PacketConn: pc, port: s.port}, nil
}

type portRedirectPacketConn struct {
	net.PacketConn
	port int
}

func (p *portRedirectPacketConn) WriteTo(b []byte, addr net.Addr) (int, error) {
	u := *addr.(*net.UDPAddr)
	u.Port = p.port
	return p.PacketConn.WriteTo(b, &u)
}

func TestOrderServers(t *testing.T) {
	ordered := OrderServers([]string{"10.0.0.1", "1.1.1.1", "9.9.9.9"}, []string{"1.1.1.1"})
	if ordered[0] != "1.1.1.1" || ordered[1] != "10.0.0.1" {
		t.Fatalf("unexpected ordering: %v", ordered)
	}
	_ = time.Second
}
