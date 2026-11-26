//go:build !windows

package nics

import (
	"context"
	"errors"
	"net"
	"time"
)

type SystemProvider struct {
	ExcludeAdapterName string
}

func (p *SystemProvider) List(ctx context.Context, includeAll bool) ([]NICInfo, error) {
	return nil, errors.New("Windows IP Helper is only available on Windows")
}

func (p *SystemProvider) GetByName(ctx context.Context, name string) (NICInfo, error) {
	return NICInfo{}, errors.New("Windows IP Helper is only available on Windows")
}

func (p *SystemProvider) DefaultPhysical(ctx context.Context) (NICInfo, error) {
	return NICInfo{}, errors.New("Windows IP Helper is only available on Windows")
}

type SystemDialer struct {
	Timeout time.Duration
}

func NewSystemDialer() *SystemDialer {
	return &SystemDialer{Timeout: 10 * time.Second}
}

func (d *SystemDialer) DialTCP(ctx context.Context, ifIndex uint32, network, addr string) (net.Conn, error) {
	return (&net.Dialer{Timeout: d.Timeout}).DialContext(ctx, network, addr)
}

func (d *SystemDialer) ListenUDP(ctx context.Context, ifIndex uint32, network string) (net.PacketConn, error) {
	laddr := "127.0.0.1:0"
	if network == "udp6" {
		laddr = "[::1]:0"
	}
	return net.ListenPacket(network, laddr)
}
