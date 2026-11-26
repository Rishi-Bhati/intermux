package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"syscall"
	"time"

	"github.com/Rishi-Bhati/intermux/windows/engine/internal/engine"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/ipc"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/nics"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/socks"
	"github.com/Rishi-Bhati/intermux/windows/engine/internal/tun"
)

func init() {
	// Lock down DLL search directories before loading any optional DLLs.
	_ = tun.InitDLLSearchPolicy()
}

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	if len(args) == 0 {
		fmt.Fprintln(os.Stderr, "usage: intermux-net <daemon|stdio|socks|check|interfaces|version|install-task|remove-task> [flags]")
		return 2
	}
	sub := args[0]
	rest := args[1:]

	switch sub {
	case "version":
		fmt.Printf("intermux-net %s (%s/%s)\n", engine.Version, runtime.GOOS, runtime.GOARCH)
		return 0

	case "check":
		return runCheck()

	case "interfaces":
		return runInterfaces(rest)

	case "stdio":
		return runStdio(rest)

	case "socks":
		return runSocks(rest)

	case "daemon":
		return runDaemon(rest)

	case "install-task":
		return runInstallTask(rest)

	case "remove-task":
		return runRemoveTask(rest)

	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", sub)
		return 2
	}
}

func runCheck() int {
	exe, _ := os.Executable()
	dirErr := ipc.VerifyAdminOnlyInstall(exe)
	wintunPath := filepath.Join(filepath.Dir(exe), "wintun.dll")
	wintunErr := tun.VerifyWintunDLL(wintunPath)
	prov := &nics.SystemProvider{ExcludeAdapterName: tun.DefaultAdapterName}
	nicList, nicErr := prov.List(context.Background(), true)

	out := map[string]any{
		"version":           engine.Version,
		"os":                runtime.GOOS,
		"arch":              runtime.GOARCH,
		"exe_path":          exe,
		"admin_dir_ok":      dirErr == nil,
		"admin_dir_error":   errStr(dirErr),
		"wintun_path":       wintunPath,
		"wintun_ok":         wintunErr == nil,
		"wintun_error":      errStr(wintunErr),
		"nic_count":         len(nicList),
		"nic_error":         errStr(nicErr),
	}
	enc, _ := json.MarshalIndent(out, "", "  ")
	fmt.Println(string(enc))
	if dirErr != nil || wintunErr != nil {
		return 1
	}
	return 0
}

func errStr(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

func runInterfaces(args []string) int {
	fs := flag.NewFlagSet("interfaces", flag.ContinueOnError)
	all := fs.Bool("all", false, "include non-bindable adapters")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	prov := &nics.SystemProvider{ExcludeAdapterName: tun.DefaultAdapterName}
	list, err := prov.List(context.Background(), *all)
	if err != nil {
		fmt.Fprintf(os.Stderr, "interfaces error: %v\n", err)
		return 1
	}
	enc, _ := json.Marshal(map[string]any{"interfaces": list})
	fmt.Println(string(enc))
	return 0
}

func runStdio(args []string) int {
	fs := flag.NewFlagSet("stdio", flag.ContinueOnError)
	stream := fs.Bool("stream", false, "keep reading lines until EOF")
	socksOnly := fs.Bool("socks-only", true, "run in unprivileged SOCKS mode")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	mode := "socks"
	if !*socksOnly {
		mode = "wintun"
	}
	exe, _ := os.Executable()
	sid, _ := ipc.CurrentUserSID()
	eng := engine.New(engine.Config{Mode: mode, EnforceSID: true})
	defer eng.Close()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	h := ipc.NewHandler(eng, exe, cancel)
	go func() {
		<-ctx.Done()
		_ = eng.Close()
	}()
	ok := ipc.ServeStdio(os.Stdin, os.Stdout, sid, h, *stream)
	if !ok {
		return 1
	}
	return 0
}

func runSocks(args []string) int {
	fs := flag.NewFlagSet("socks", flag.ContinueOnError)
	iface := fs.String("iface", "", "target interface name")
	listen := fs.String("listen", "127.0.0.1:0", "loopback SOCKS5 listen address")
	loopbackIPC := fs.String("loopback-ipc", "127.0.0.1:0", "loopback IPC address")
	tokenFile := fs.String("token-file", "", "token output file")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *iface == "" {
		fmt.Fprintln(os.Stderr, "--iface is required")
		return 2
	}

	exe, _ := os.Executable()
	sid, _ := ipc.CurrentUserSID()
	eng := engine.New(engine.Config{Mode: "socks", EnforceSID: true})
	defer eng.Close()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	h := ipc.NewHandler(eng, exe, cancel)

	res, err := eng.BindRegister("socks-main", *iface, "socks", nil, sid, "keep", nil, nil, true)
	if err != nil {
		fmt.Fprintf(os.Stderr, "socks init failed: %v\n", err)
		return 1
	}

	ipcSrv, err := ipc.StartLoopback(ctx, *loopbackIPC, *tokenFile, sid, h)
	if err != nil {
		fmt.Fprintf(os.Stderr, "loopback IPC failed: %v\n", err)
		return 1
	}
	defer ipcSrv.Close()

	_ = listen
	ready := map[string]any{
		"ipc_addr":   ipcSrv.Addr(),
		"socks_port": res["socks_port"],
		"socks_auth": res["socks_auth"],
	}
	if cred, ok := res["socks_auth"].(*socks.Credential); ok && cred != nil {
		ready["socks_auth"] = cred
	}
	b, _ := json.Marshal(ready)
	fmt.Println(string(b))

	<-ctx.Done()
	return 0
}

func runDaemon(args []string) int {
	fs := flag.NewFlagSet("daemon", flag.ContinueOnError)
	pipePath := fs.String("pipe", "", "Named Pipe path (defaults to user-scoped pipe)")
	allowedSID := fs.String("allowed-sid", "", "authorized caller Windows SID")
	mtu := fs.Uint("mtu", tun.DefaultMTU, "Wintun MTU")
	socksFallback := fs.Bool("socks-fallback", false, "run without Wintun in SOCKS5 mode")
	if err := fs.Parse(args); err != nil {
		return 2
	}

	exe, err := os.Executable()
	if err != nil {
		fmt.Fprintf(os.Stderr, "cannot determine executable path: %v\n", err)
		return 1
	}

	// Refuse to run elevated from a user-writable directory (e.g. %LOCALAPPDATA%).
	if !*socksFallback {
		if err := ipc.VerifyAdminOnlyInstall(exe); err != nil {
			fmt.Fprintf(os.Stderr, "refusing to start elevated engine: %v\n", err)
			return 1
		}
	}

	sid := *allowedSID
	if sid == "" {
		sid, err = ipc.CurrentUserSID()
		if err != nil {
			fmt.Fprintf(os.Stderr, "cannot determine caller SID: %v\n", err)
			return 1
		}
	}
	if _, err := ipc.ValidateSID(sid); err != nil {
		fmt.Fprintf(os.Stderr, "invalid --allowed-sid: %v\n", err)
		return 1
	}

	eng := engine.New(engine.Config{
		Mode:       "wintun",
		EnforceSID: true,
	})
	defer func() {
		if r := recover(); r != nil {
			fmt.Fprintf(os.Stderr, "panic in intermux-net: %v\n", r)
			_ = eng.Close()
			os.Exit(1)
		}
		_ = eng.Close()
	}()

	if !*socksFallback {
		if err := eng.StartWintun(uint32(*mtu)); err != nil {
			fmt.Fprintf(os.Stderr, "Wintun initialization failed: %v\n", err)
			return 1
		}
	}

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	h := ipc.NewHandler(eng, exe, cancel)

	pipeErrCh := make(chan error, 1)
	go func() {
		pipeErrCh <- ipc.ServeNamedPipe(ctx, *pipePath, sid, h)
	}()

	select {
	case <-ctx.Done():
		shutdownCtx, scancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer scancel()
		_ = shutdownCtx
		_ = eng.Close()
		return 0
	case perr := <-pipeErrCh:
		_ = eng.Close()
		if perr != nil {
			fmt.Fprintf(os.Stderr, "pipe server error: %v\n", perr)
			return 1
		}
		return 0
	}
}

func runInstallTask(args []string) int {
	fs := flag.NewFlagSet("install-task", flag.ContinueOnError)
	sidFlag := fs.String("allowed-sid", "", "caller Windows SID")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	exe, err := os.Executable()
	if err != nil {
		fmt.Fprintf(os.Stderr, "executable error: %v\n", err)
		return 1
	}
	if err := ipc.InstallPasswordlessTask(exe, *sidFlag); err != nil {
		fmt.Fprintf(os.Stderr, "install-task failed: %v\n", err)
		return 1
	}
	return 0
}

func runRemoveTask(args []string) int {
	fs := flag.NewFlagSet("remove-task", flag.ContinueOnError)
	sidFlag := fs.String("allowed-sid", "", "caller Windows SID")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if err := ipc.RemovePasswordlessTask(*sidFlag); err != nil {
		fmt.Fprintf(os.Stderr, "remove-task failed: %v\n", err)
		return 1
	}
	return 0
}
