# InterMux Windows Security Architecture

This document records the enforced trust boundaries for the Windows edition of InterMux (`InterMux.exe` + `intermux-net.exe` + `wintun.dll`).

## 1. Privilege Boundary & Process Isolation

- **Unprivileged GUI, CLI, and Launched Applications**:
  - `InterMux.exe` runs **unprivileged** (`uac_admin=False` in `windows/intermux_windows.spec`).
  - All bound applications (including browsers) are spawned **unprivileged** as the normal desktop user from the GUI/CLI (`CreateProcess`). Never launch user applications with an elevated Administrator token.
- **Admin-Only Installation Directory for the Elevated Engine**:
  - Only `intermux-net.exe` elevates via `ShellExecuteW(..., "runas", ...)` when privileged Wintun/route operations are needed.
  - Elevated `intermux-net.exe` and `wintun.dll` MUST reside in an Administrator-only directory (`%ProgramFiles%\InterMux\`, writable only by `Administrators` and `SYSTEM`).
  - The engine MUST NEVER be elevated from a user-writable directory such as `%LOCALAPPDATA%` (which would enable silent UAC bypass / binary planting by unprivileged processes).
  - On startup in elevated/daemon modes (`daemon`, `install-task`), `intermux-net.exe` inspects the DACL of both its own executable image and its parent directory and **refuses to run** if any non-admin SID (`Users`, `Authenticated Users`, `Everyone`, etc.) holds write, modify, or full-control permissions.
- **Passwordless Mode (Scheduled Task)**:
  - Opt-in passwordless elevation uses a per-user Windows Scheduled Task (`\InterMux\Helper-<SID>`) running with `HighestAvailable` privileges.
  - Its action is the fixed absolute `%ProgramFiles%\InterMux\intermux-net.exe` path with only fixed arguments (`daemon --allowed-sid <SID>` — zero user-controlled command arguments).
  - Task XML registration and deletion are performed exclusively through the elevated helper after verifying caller identity.
- **Typed IPC Operations Only**:
  - `intermux-net.exe` accepts only strongly typed, schema-validated JSON v1 actions (`ping`, `interfaces`, `status`, `bind_register`, `bind_unregister`, `adopt_pids`, `release_adoption`, `set_dns`, `set_limit`, `counters`, `verify`, `reconnect`, `teardown_idle`, `reset`, `shutdown`, `passwordless_enable`, `passwordless_disable`).
  - There is NO arbitrary command execution, shell invocation, or "launch executable as admin" endpoint.
- **Same-SID Process Ownership & Inheritance**:
  - Before registering, adopting, stopping, or throttling any PID, `intermux-net.exe` resolves the pipe client's token user SID (`GetNamedPipeClientProcessId` -> `OpenProcessToken` -> `TokenUser`) and verifies the target PID's token user SID matches identically.
  - Cross-SID PID registration or control is refused (`permission_denied`).
  - Child-process inheritance in `pidmap` applies **only** when the child process token user SID matches the owning rule's SID.

## 2. Named Pipe ACLs, Anti-Squatting & Loopback Auth

- **User-Scoped Named Pipe**:
  - Pipe path: `\\.\pipe\intermux-net-<shortSID>` (derived deterministically from the owning user's SID).
  - Created with `FILE_FLAG_FIRST_PIPE_INSTANCE` (`0x00080000`) and `PIPE_REJECT_REMOTE_CLIENTS` (`0x00000008`). If `CreateNamedPipeW` fails (e.g., an instance already exists), the helper exits with an error — never falling back to an alternate pipe name.
  - Protected DACL (`D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;<callerSID>)`): grants Generic All only to `LOCAL SYSTEM` (`SY`), `BUILTIN\Administrators` (`BA`), and the specific `<callerSID>`. `Everyone` (`WD`), `Authenticated Users` (`AU`), `Interactive` (`IU`), and `Network` (`NU`) are strictly excluded.
- **Mutual Server Authentication by the Client**:
  - Because `FILE_FLAG_FIRST_PIPE_INSTANCE` protects only a server creating the first instance and a rogue user-level squatter could race to pre-create the pipe before elevation, the Python client MUST authenticate the pipe server immediately after connecting:
    1. Call `GetNamedPipeServerProcessId` on the connected pipe handle.
    2. Open the server process (`PROCESS_QUERY_LIMITED_INFORMATION`) and verify its full image path (`QueryFullProcessImageNameW`) matches the installed `%ProgramFiles%\InterMux\intermux-net.exe`.
    3. Verify the server process token is elevated (`GetTokenInformation(TokenElevation)` != 0).
    4. If any check fails, close the handle immediately and raise `PermissionDeniedError`.
- **Unprivileged Loopback Fallback**:
  - Loopback IPC binds `127.0.0.1` only. Its nonce token file (`%LOCALAPPDATA%\InterMux\ipc.token`) is protected with a user-only DACL (`SY`, `BA`, and `<userSID>`, `D:P`), and authentication checks use `crypto/subtle.ConstantTimeCompare`.
  - The unprivileged SOCKS5 fallback listener binds `127.0.0.1` only and **requires RFC 1929 username/password authentication (`METHOD 0x02`)** with a per-session `crypto/rand` credential (unauthenticated `0x00` is rejected), preventing other local users on `127.0.0.1` from hijacking the proxy.

## 3. Wintun Driver & DLL Loading Security

- **DLL Search-Order Protection**:
  - At the very start of `intermux-net.exe`, call `SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32)`.
  - Load `wintun.dll` using `LoadLibraryExW` on the absolute path `<exe_dir>\wintun.dll` with flags `LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32`, invoking all Wintun API procedures directly from that verified `HMODULE`.
- **Cryptographic Hash & Authenticode Verification**:
  - `scripts/fetch-wintun.ps1` pins exact SHA-256 checksums for `wintun-0.14.1.zip` and each architecture's `wintun.dll` (`amd64`, `arm64`, `x86`), plus verifies `Get-AuthenticodeSignature` signer `"WireGuard LLC"`.
  - `intermux-net.exe` embeds the pinned SHA-256 digest for each architecture at build time, computes the SHA-256 of `<exe_dir>\wintun.dll` before loading it, and runs `WinVerifyTrust` (`WINTRUST_ACTION_GENERIC_VERIFY_V2`) checking the `"WireGuard LLC"` signer where available. Any mismatch or verification failure refuses to load Wintun.
