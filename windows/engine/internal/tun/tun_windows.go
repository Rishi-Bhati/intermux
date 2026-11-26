//go:build windows

package tun

import (
	"crypto/md5"
	"errors"
	"fmt"
	"io"
	"net/netip"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"golang.zx2c4.com/wireguard/windows/tunnel/winipcfg"
)

const (
	loadLibrarySearchDLLLoadDir  = 0x00000100
	loadLibrarySearchSystem32    = 0x00000800
	wtdUINone                    = 2
	wtdRevokeNone                = 0
	wtdChoiceFile                = 1
	wtdStateActionVerify         = 1
	wtdStateActionClose          = 2
	certQueryObjectFile          = 1
	certQueryContentFlagPKCS7Sig = 1 << 10
	certQueryFormatFlagAll       = 1 << 1 | 1 << 2 | 1 << 3
	certNameSimpleDisplayType    = 4
)

var (
	kernel32                    = syscall.NewLazyDLL("kernel32.dll")
	procSetDefaultDllDirectories = kernel32.NewProc("SetDefaultDllDirectories")
	procLoadLibraryExW          = kernel32.NewProc("LoadLibraryExW")

	wintrust           = syscall.NewLazyDLL("wintrust.dll")
	procWinVerifyTrust = wintrust.NewProc("WinVerifyTrust")

	crypt32              = syscall.NewLazyDLL("crypt32.dll")
	procCryptQueryObject = crypt32.NewProc("CryptQueryObject")
	procCertGetNameStrW  = crypt32.NewProc("CertGetNameStringW")
	procCertFreeContext  = crypt32.NewProc("CertFreeCertificateContext")
	procCertCloseStore   = crypt32.NewProc("CertCloseStore")
)

var (
	wintunMu     sync.Mutex
	wintunMod    windows.Handle
	wintunProcs  struct {
		createAdapter        uintptr
		openAdapter          uintptr
		closeAdapter         uintptr
		getAdapterLUID       uintptr
		startSession         uintptr
		endSession           uintptr
		getReadWaitEvent     uintptr
		receivePacket        uintptr
		releaseReceivePacket uintptr
		allocateSendPacket   uintptr
		sendPacket           uintptr
	}
)

func initDLLSearchPolicyPlatform() error {
	if err := procSetDefaultDllDirectories.Find(); err != nil {
		return err
	}
	r1, _, e1 := procSetDefaultDllDirectories.Call(uintptr(loadLibrarySearchSystem32))
	if r1 == 0 {
		return fmt.Errorf("SetDefaultDllDirectories failed: %w", e1)
	}
	return nil
}

type winTrustFileInfo struct {
	cbStruct       uint32
	pcwszFilePath  *uint16
	hFile          windows.Handle
	pgKnownSubject *windows.GUID
}

type winTrustData struct {
	cbStruct            uint32
	pPolicyCallbackData uintptr
	pSIPClientData      uintptr
	dwUIChoice          uint32
	fdwRevocationChecks uint32
	dwUnionChoice       uint32
	pFile               *winTrustFileInfo
	dwStateAction       uint32
	hWVTStateData       windows.Handle
	pwszURLReference    *uint16
	dwProvFlags         uint32
	dwUIContext         uint32
	pSignatureSettings  uintptr
}

var winTrustActionGenericVerifyV2 = windows.GUID{
	Data1: 0x00aac56b,
	Data2: 0xcd44,
	Data3: 0x11d0,
	Data4: [8]byte{0x8c, 0xc2, 0x00, 0xc0, 0x4f, 0xc2, 0x95, 0xee},
}

func verifyWintunAuthenticodePlatform(dllPath string) error {
	pathPtr, err := windows.UTF16PtrFromString(dllPath)
	if err != nil {
		return err
	}
	fileInfo := winTrustFileInfo{
		cbStruct:      uint32(unsafe.Sizeof(winTrustFileInfo{})),
		pcwszFilePath: pathPtr,
	}
	data := winTrustData{
		cbStruct:            uint32(unsafe.Sizeof(winTrustData{})),
		dwUIChoice:          wtdUINone,
		fdwRevocationChecks: wtdRevokeNone,
		dwUnionChoice:       wtdChoiceFile,
		pFile:               &fileInfo,
		dwStateAction:       wtdStateActionVerify,
	}
	status, _, _ := procWinVerifyTrust.Call(
		0,
		uintptr(unsafe.Pointer(&winTrustActionGenericVerifyV2)),
		uintptr(unsafe.Pointer(&data)),
	)
	data.dwStateAction = wtdStateActionClose
	_, _, _ = procWinVerifyTrust.Call(
		0,
		uintptr(unsafe.Pointer(&winTrustActionGenericVerifyV2)),
		uintptr(unsafe.Pointer(&data)),
	)
	if status != 0 {
		return fmt.Errorf("WinVerifyTrust failed for %s (0x%x)", dllPath, uint32(status))
	}

	// Also verify signer subject name contains "WireGuard LLC" via CryptQueryObject if available.
	if err := procCryptQueryObject.Find(); err == nil {
		var encoding, contentType, formatType uint32
		var store, msg, ctxPtr uintptr
		r1, _, _ := procCryptQueryObject.Call(
			certQueryObjectFile,
			uintptr(unsafe.Pointer(pathPtr)),
			certQueryContentFlagPKCS7Sig,
			certQueryFormatFlagAll,
			0,
			uintptr(unsafe.Pointer(&encoding)),
			uintptr(unsafe.Pointer(&contentType)),
			uintptr(unsafe.Pointer(&formatType)),
			uintptr(unsafe.Pointer(&store)),
			uintptr(unsafe.Pointer(&msg)),
			uintptr(unsafe.Pointer(&ctxPtr)),
		)
		if r1 != 0 && store != 0 {
			defer procCertCloseStore.Call(store, 0)
			if ctxPtr != 0 {
				defer procCertFreeContext.Call(ctxPtr)
			}
			// Enumerate certs in embedded PKCS7 store looking for WireGuard LLC subject.
			procCertEnum := crypt32.NewProc("CertEnumCertificatesInStore")
			if procCertEnum.Find() == nil {
				var cur uintptr
				matched := false
				for {
					cur, _, _ = procCertEnum.Call(store, cur)
					if cur == 0 {
						break
					}
					buf := make([]uint16, 256)
					n, _, _ := procCertGetNameStrW.Call(cur, certNameSimpleDisplayType, 0, 0, uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)))
					if n > 0 && strings.Contains(windows.UTF16ToString(buf[:n]), "WireGuard LLC") {
						matched = true
						procCertFreeContext.Call(cur)
						break
					}
				}
				if !matched {
					return fmt.Errorf("wintun.dll Authenticode signer is not WireGuard LLC")
				}
			}
		}
	}
	return nil
}

func loadVerifiedWintun() error {
	wintunMu.Lock()
	defer wintunMu.Unlock()
	if wintunMod != 0 {
		return nil
	}
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	absDll := filepath.Join(filepath.Dir(exe), "wintun.dll")
	if err := VerifyWintunDLL(absDll); err != nil {
		return err
	}
	ptr, err := windows.UTF16PtrFromString(absDll)
	if err != nil {
		return err
	}
	h, _, e1 := procLoadLibraryExW.Call(
		uintptr(unsafe.Pointer(ptr)),
		0,
		uintptr(loadLibrarySearchDLLLoadDir|loadLibrarySearchSystem32),
	)
	if h == 0 {
		return fmt.Errorf("LoadLibraryExW(%s) failed: %w", absDll, e1)
	}
	mod := windows.Handle(h)
	resolve := func(name string) (uintptr, error) {
		p, err := windows.GetProcAddress(mod, name)
		if err != nil {
			return 0, fmt.Errorf("missing Wintun export %s: %w", name, err)
		}
		return p, nil
	}
	var errProc error
	if wintunProcs.createAdapter, errProc = resolve("WintunCreateAdapter"); errProc != nil {
		return errProc
	}
	if wintunProcs.openAdapter, errProc = resolve("WintunOpenAdapter"); errProc != nil {
		return errProc
	}
	if wintunProcs.closeAdapter, errProc = resolve("WintunCloseAdapter"); errProc != nil {
		return errProc
	}
	if wintunProcs.getAdapterLUID, errProc = resolve("WintunGetAdapterLUID"); errProc != nil {
		return errProc
	}
	if wintunProcs.startSession, errProc = resolve("WintunStartSession"); errProc != nil {
		return errProc
	}
	if wintunProcs.endSession, errProc = resolve("WintunEndSession"); errProc != nil {
		return errProc
	}
	if wintunProcs.getReadWaitEvent, errProc = resolve("WintunGetReadWaitEvent"); errProc != nil {
		return errProc
	}
	if wintunProcs.receivePacket, errProc = resolve("WintunReceivePacket"); errProc != nil {
		return errProc
	}
	if wintunProcs.releaseReceivePacket, errProc = resolve("WintunReleaseReceivePacket"); errProc != nil {
		return errProc
	}
	if wintunProcs.allocateSendPacket, errProc = resolve("WintunAllocateSendPacket"); errProc != nil {
		return errProc
	}
	if wintunProcs.sendPacket, errProc = resolve("WintunSendPacket"); errProc != nil {
		return errProc
	}
	wintunMod = mod
	return nil
}

func retryWinIP(fn func() error) error {
	var err error
	for i := 0; i < 20; i++ {
		err = fn()
		if err == nil {
			return nil
		}
		if !errors.Is(err, windows.ERROR_NOT_FOUND) {
			return err
		}
		time.Sleep(50 * time.Millisecond)
	}
	return err
}

func deterministicGUID(name string) windows.GUID {
	sum := md5.Sum([]byte("InterMux-Wintun-Adapter-v1:" + name))
	var g windows.GUID
	g.Data1 = uint32(sum[0])<<24 | uint32(sum[1])<<16 | uint32(sum[2])<<8 | uint32(sum[3])
	g.Data2 = uint16(sum[4])<<8 | uint16(sum[5])
	g.Data3 = (uint16(sum[6])<<8 | uint16(sum[7]))&0x0fff | 0x4000
	copy(g.Data4[:], sum[8:16])
	g.Data4[0] = (g.Data4[0] & 0x3f) | 0x80
	return g
}

type winDevice struct {
	name       string
	mtu        uint32
	luid       winipcfg.LUID
	ifIndex    uint32
	addrV4     netip.Prefix
	addrV6     netip.Prefix
	enableIPv6 bool
	adapter    uintptr
	session    uintptr
	readEvent  windows.Handle
	closeEvent windows.Handle
	closeOnce  sync.Once
	routesV4   []*winipcfg.RouteData
	routesV6   []*winipcfg.RouteData
}

func sweepStalePlatform(adapterName string) error {
	if adapterName == "" {
		adapterName = DefaultAdapterName
	}
	for _, fam := range []winipcfg.AddressFamily{windows.AF_INET, windows.AF_INET6} {
		routes, err := winipcfg.GetIPForwardTable2(fam)
		if err != nil {
			continue
		}
		for _, r := range routes {
			if r.DestinationPrefix.PrefixLength != 1 {
				continue
			}
			row, ierr := r.InterfaceLUID.Interface()
			if ierr != nil || row == nil || strings.EqualFold(row.Alias(), adapterName) {
				_ = r.Delete()
			}
		}
	}
	if err := loadVerifiedWintun(); err == nil {
		namePtr, _ := windows.UTF16PtrFromString(adapterName)
		if h, _, _ := syscall.SyscallN(wintunProcs.openAdapter, uintptr(unsafe.Pointer(namePtr))); h != 0 {
			_, _, _ = syscall.SyscallN(wintunProcs.closeAdapter, h)
		}
	}
	return nil
}

func openPlatform(cfg Config) (Device, error) {
	if err := loadVerifiedWintun(); err != nil {
		return nil, err
	}
	name := cfg.AdapterName
	if name == "" {
		name = DefaultAdapterName
	}
	mtu := cfg.MTU
	if mtu == 0 {
		mtu = DefaultMTU
	}
	ringCap := cfg.RingCap
	if ringCap == 0 {
		ringCap = DefaultRingCap
	}
	_ = sweepStalePlatform(name)

	v4Prefix, v6Prefix, err := PickSubnets(cfg.ExcludeV4, cfg.ExcludeV6)
	if err != nil {
		return nil, err
	}

	namePtr, err := windows.UTF16PtrFromString(name)
	if err != nil {
		return nil, err
	}
	typePtr, err := windows.UTF16PtrFromString(DefaultTunnelType)
	if err != nil {
		return nil, err
	}
	guid := deterministicGUID(name)

	adapter, _, e1 := syscall.SyscallN(
		wintunProcs.createAdapter,
		uintptr(unsafe.Pointer(namePtr)),
		uintptr(unsafe.Pointer(typePtr)),
		uintptr(unsafe.Pointer(&guid)),
	)
	if adapter == 0 {
		return nil, fmt.Errorf("WintunCreateAdapter failed: %w", e1)
	}

	var rawLUID uint64
	_, _, _ = syscall.SyscallN(wintunProcs.getAdapterLUID, adapter, uintptr(unsafe.Pointer(&rawLUID)))
	luid := winipcfg.LUID(rawLUID)

	// Start the Wintun session before configuring routes so Windows transitions
	// the virtual adapter media state to connected and binds Tcpip/NDIS cleanly.
	session, _, e2 := syscall.SyscallN(wintunProcs.startSession, adapter, uintptr(ringCap))
	if session == 0 {
		_, _, _ = syscall.SyscallN(wintunProcs.closeAdapter, adapter)
		return nil, fmt.Errorf("WintunStartSession failed: %w", e2)
	}
	cleanupOnErr := func() {
		_, _, _ = syscall.SyscallN(wintunProcs.endSession, session)
		_, _, _ = syscall.SyscallN(wintunProcs.closeAdapter, adapter)
	}

	if err := retryWinIP(func() error {
		return luid.SetIPAddressesForFamily(windows.AF_INET, []netip.Prefix{v4Prefix})
	}); err != nil {
		cleanupOnErr()
		return nil, fmt.Errorf("SetIPAddressesForFamily v4 failed: %w", err)
	}
	if ipif4, err := luid.IPInterface(windows.AF_INET); err == nil && ipif4 != nil {
		ipif4.NLMTU = mtu
		ipif4.UseAutomaticMetric = false
		ipif4.Metric = 5
		_ = ipif4.Set()
	}

	routesV4 := []*winipcfg.RouteData{
		{Destination: netip.MustParsePrefix("0.0.0.0/1"), NextHop: netip.IPv4Unspecified(), Metric: 0},
		{Destination: netip.MustParsePrefix("128.0.0.0/1"), NextHop: netip.IPv4Unspecified(), Metric: 0},
	}
	for _, rd := range routesV4 {
		rd := rd
		if err := retryWinIP(func() error {
			aerr := luid.AddRoute(rd.Destination, rd.NextHop, rd.Metric)
			if errors.Is(aerr, windows.ERROR_OBJECT_ALREADY_EXISTS) {
				return nil
			}
			return aerr
		}); err != nil {
			for _, del := range routesV4 {
				_ = luid.DeleteRoute(del.Destination, del.NextHop)
			}
			cleanupOnErr()
			return nil, fmt.Errorf("AddRoute %s failed: %w", rd.Destination, err)
		}
	}

	var routesV6 []*winipcfg.RouteData
	if cfg.EnableIPv6 {
		if err := luid.SetIPAddressesForFamily(windows.AF_INET6, []netip.Prefix{v6Prefix}); err == nil {
			if ipif6, err := luid.IPInterface(windows.AF_INET6); err == nil && ipif6 != nil {
				ipif6.NLMTU = mtu
				ipif6.UseAutomaticMetric = false
				ipif6.Metric = 5
				_ = ipif6.Set()
			}
			routesV6 = []*winipcfg.RouteData{
				{Destination: netip.MustParsePrefix("::/1"), NextHop: netip.IPv6Unspecified(), Metric: 0},
				{Destination: netip.MustParsePrefix("8000::/1"), NextHop: netip.IPv6Unspecified(), Metric: 0},
			}
			for _, rd := range routesV6 {
				aerr := luid.AddRoute(rd.Destination, rd.NextHop, rd.Metric)
				if errors.Is(aerr, windows.ERROR_OBJECT_ALREADY_EXISTS) {
					continue
				}
			}
		}
	}

	// Point Wintun DNS at its gateway IP so DNS queries are captured inside Wintun.
	if v4Prefix.Addr().Is4() {
		b := v4Prefix.Addr().As4()
		b[3] = 2
		_ = luid.SetDNS(windows.AF_INET, []netip.Addr{netip.AddrFrom4(b)}, nil)
	}

	readEvt, _, _ := syscall.SyscallN(wintunProcs.getReadWaitEvent, session)
	closeEvt, err := windows.CreateEvent(nil, 1, 0, nil)
	if err != nil {
		for _, del := range routesV4 {
			_ = luid.DeleteRoute(del.Destination, del.NextHop)
		}
		cleanupOnErr()
		return nil, err
	}

	var ifIdx uint32
	if row, err := luid.Interface(); err == nil && row != nil {
		ifIdx = row.InterfaceIndex
	}

	return &winDevice{
		name:       name,
		mtu:        mtu,
		luid:       luid,
		ifIndex:    ifIdx,
		addrV4:     v4Prefix,
		addrV6:     v6Prefix,
		enableIPv6: cfg.EnableIPv6,
		adapter:    adapter,
		session:    session,
		readEvent:  windows.Handle(readEvt),
		closeEvent: closeEvt,
		routesV4:   routesV4,
		routesV6:   routesV6,
	}, nil
}

func (d *winDevice) Name() string        { return d.name }
func (d *winDevice) MTU() uint32         { return d.mtu }
func (d *winDevice) LUID() uint64        { return uint64(d.luid) }
func (d *winDevice) IfIndex() uint32     { return d.ifIndex }
func (d *winDevice) AddrV4() netip.Prefix { return d.addrV4 }
func (d *winDevice) AddrV6() netip.Prefix { return d.addrV6 }

func (d *winDevice) Read(p []byte) (int, error) {
	for {
		var pktSize uint32
		ptr, _, e1 := syscall.SyscallN(wintunProcs.receivePacket, d.session, uintptr(unsafe.Pointer(&pktSize)))
		if ptr != 0 {
			n := int(pktSize)
			if n > len(p) {
				_, _, _ = syscall.SyscallN(wintunProcs.releaseReceivePacket, d.session, ptr)
				return 0, io.ErrShortBuffer
			}
			src := unsafe.Slice((*byte)(unsafe.Pointer(ptr)), n)
			copy(p[:n], src)
			_, _, _ = syscall.SyscallN(wintunProcs.releaseReceivePacket, d.session, ptr)
			runtime.KeepAlive(p)
			return n, nil
		}
		if e1 != windows.ERROR_NO_MORE_ITEMS {
			return 0, fmt.Errorf("WintunReceivePacket failed: %w", e1)
		}
		handles := []windows.Handle{d.readEvent, d.closeEvent}
		event, err := windows.WaitForMultipleObjects(handles, false, windows.INFINITE)
		if err != nil || event == windows.WAIT_OBJECT_0+1 {
			return 0, os.ErrClosed
		}
	}
}

func (d *winDevice) Write(p []byte) (int, error) {
	if len(p) == 0 {
		return 0, nil
	}
	ptr, _, e1 := syscall.SyscallN(wintunProcs.allocateSendPacket, d.session, uintptr(len(p)))
	if ptr == 0 {
		if e1 == windows.ERROR_BUFFER_OVERFLOW {
			return len(p), nil
		}
		return 0, fmt.Errorf("WintunAllocateSendPacket failed: %w", e1)
	}
	dst := unsafe.Slice((*byte)(unsafe.Pointer(ptr)), len(p))
	copy(dst, p)
	_, _, _ = syscall.SyscallN(wintunProcs.sendPacket, d.session, ptr)
	runtime.KeepAlive(p)
	return len(p), nil
}

func (d *winDevice) CleanupRoutes() error {
	var errs []error
	for _, r := range d.routesV4 {
		if err := d.luid.DeleteRoute(r.Destination, r.NextHop); err != nil {
			errs = append(errs, err)
		}
	}
	for _, r := range d.routesV6 {
		if err := d.luid.DeleteRoute(r.Destination, r.NextHop); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

func (d *winDevice) Close() error {
	d.closeOnce.Do(func() {
		_ = windows.SetEvent(d.closeEvent)
		_ = d.CleanupRoutes()
		_, _, _ = syscall.SyscallN(wintunProcs.endSession, d.session)
		_, _, _ = syscall.SyscallN(wintunProcs.closeAdapter, d.adapter)
		_ = windows.CloseHandle(d.closeEvent)
	})
	return nil
}
