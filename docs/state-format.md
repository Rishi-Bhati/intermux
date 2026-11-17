# state.json format

`/var/lib/intermux/state.json` (`intermux/config.py: STATE_FILE`) is the record of what InterMux has set up:
the namespaces, the bindings, the running apps moved into a routing group, and the per-interface DNS and
speed limits. The reference implementation is `intermux/state.py`. This document describes that code as it
is in release 2.2.0 (tag `linux-v2.2.0`). If the code and this document disagree, the code wins and this
document must be fixed.

Format version: the top-level `version` key is `1`. Nothing branches on it. See "Compatibility" below.

## Who reads and writes it

| Process | Access | Use |
|---|---|---|
| Root helper (`intermux helper`, through `engine.py`) | Reads and writes, under the state lock | Every action that changes the setup |
| Root watcher (`intermux watch`, `watch.py`) | Reads, and writes under the state lock when it tears down or re-adds routes | Removes idle bindings; re-adds routes |
| Window and CLI (`intermux`, as the user) | Read only | Shows bindings and status; `intermux status`, the bug report |
| `intermuxd` (Rust, in progress in `rust/intermuxd`) | Read only; inotify on `/var/lib/intermux/` | Binding and adoption events |

No process other than the root helper and the root watcher may write this file. Other readers must not
write it, not even to repair it.

## File, directory and permissions

- Directory `/var/lib/intermux`, created with mode `0755` when first needed.
- File `state.json`, owner root, mode `0644`. Any local user can read it. This is by design and is stated
  in SECURITY.md ("Bound apps are visible to other users").
- Writes are atomic. `save_state` writes the JSON to a temporary file `.state.json.<random>.tmp` in the same
  directory (created `0600`), calls `fsync`, sets mode `0644`, and then calls `os.replace` onto `state.json`.
  A reader sees either the old file or the new one. Readers that scan the directory should skip names that
  start with `.state.json.`.
- Writers hold an exclusive, blocking `flock` on `/run/intermux/helper.lock` (`config.LOCK_FILE`). The
  directory `/run/intermux` is mode `0700` and root-owned. The helper takes the lock for every action except
  `status`. The watcher takes it for teardown and route refresh.
- Readers take no lock. They depend on the atomic replace.
- A missing file loads as an empty state.
- A file that cannot be read (`OSError`) loads as an empty state. The file is not moved.
- A file that is not valid UTF-8 JSON, or whose top-level value is not an object, is renamed to
  `state.json.corrupt-<unix time>` when the process can write the directory (in practice, the root helper or
  watcher). The process then continues with an empty state. A reader that cannot write the directory logs a
  warning, keeps the file, and continues with an empty state.

## Top-level keys

`save_state` writes every key below, with `sort_keys=True` and `indent=2`, and a final newline.

| Key | Type | Added in | Meaning |
|---|---|---|---|
| `version` | integer | 2.0.0 | Always written as `1`. Read with `int()`; defaults to `1` when missing or not convertible. |
| `subnets` | object: interface name to CIDR string | 2.0.0 | The `10.200.N.0/24` subnet of each interface's namespace. Removed with the namespace. |
| `bindings` | array of Binding | 2.0.0 | Applications started by InterMux inside an interface's namespace. |
| `adoptions` | array of Adoption | 2.0.0 | Running applications moved into a routing group (`intermux adopt`, "Shift current session"). |
| `ip_forward_prev` | string or null | 2.0.0 | The value of `net.ipv4.ip_forward` seen before InterMux first enabled forwarding. Recorded once and not overwritten. Restored to the kernel when the last binding is removed. |
| `dns` | object: interface name to array of IPv4 strings | 2.1.0 | DNS servers chosen by hand for the interface's namespace. Absent or empty means Automatic. |
| `limits` | object: interface name to `{"down": number or null, "up": number or null}` | 2.1.0 | Speed limit of the interface's namespace, in Mbit/s. An entry with both values null is not kept. |

## Binding

One element of `bindings`. Fields are written in alphabetical order.

| Field | Type | Added in | Meaning and rules |
|---|---|---|---|
| `iface` | string | 2.0.0 | Interface name. Must match `^[A-Za-z0-9_.:@-]{1,15}$` and must not be `.` or `..`. |
| `app` | string | 2.0.0 | Absolute path of the application's binary. The loader does not check that it is absolute. |
| `app_name` | string | 2.0.0 | Basename of `app`. Defaults to the basename when missing. |
| `namespace` | string | 2.0.0 | Network namespace name: `ns_` followed by 8 lowercase hex digits (`^ns_[0-9a-f]{8}$`). |
| `subnet` | string | 2.0.0 | Canonical `/24` inside `10.200.0.0/16` whose third octet N is 1 to 254, for example `10.200.7.0/24`. |
| `table` | integer | 2.0.0 | Routing table: `1000 + N`, where N is the third octet of `subnet`. Must be 1 to 2147483647. |
| `priority` | integer | 2.0.0 | `ip rule` priority: `1000 + N`. Must be 1 to 32765. |
| `profile_mode` | string | 2.0.0 | `"default"` or `"separate"` (a separate browser or Firefox profile). Defaults to `"default"` when empty. |
| `log_path` | string | 2.0.0 | Path of the application's log file under the user's home, normally `~/.local/state/intermux/logs/`. Empty when unknown. |
| `created_at` | string | 2.0.0 | ISO 8601 UTC time, to the second, for example `2026-11-18T10:15:42+00:00`. |
| `pid` | integer or null | 2.0.0 | PID of the launched process. Used for liveness. Null when absent, boolean, or 1 or less. |
| `uid` | integer or null | 2.2.0 | UID of the user who created the binding. Must be a non-negative integer, not a boolean. Null in files written before 2.2.0. |

Owner rule: when `uid` is null, the owner is read from the live process `pid` while that process is still in
the binding's namespace. If that cannot be done, the owner is unknown and the owner check is not applied.

## Adoption

One element of `adoptions`. A running app that InterMux moved into a cgroup v2 group and routed with an
nftables mark. Fields are written in alphabetical order.

| Field | Type | Added in | Meaning and rules |
|---|---|---|---|
| `id` | string | 2.0.0 | 8 to 32 lowercase hex characters (`^[0-9a-f]{8,32}$`). Shown by `intermux status` and used by `intermux release`. |
| `iface` | string | 2.0.0 | Interface name, same rule as Binding. |
| `app` | string | 2.0.0 | Absolute path of the application's binary. The loader does not check that it is absolute. |
| `app_name` | string | 2.0.0 | Basename of `app`. Defaults to the basename when missing. |
| `slot` | integer | 2.0.0 | 1 to 200. Selects the nftables table `intermux_adopt<slot>`, the fwmark and the routing table. |
| `group` | string | 2.0.0 | Must be exactly `intermux/adopt-` followed by `id`. This is the cgroup v2 group, relative to the cgroup root. |
| `mark` | integer | 2.0.0 | fwmark: `0x100 + slot`. For slot 5, this is 261. |
| `table` | integer | 2.0.0 | Routing table: `2000 + slot`. |
| `priority` | integer | 2.0.0 | `ip rule` priority: `2000 + slot`. |
| `uid` | integer | 2.0.0 | UID of the owner. Required. The boolean value `true` is rejected. |
| `pids` | array of integers | 2.0.0 | PIDs moved into the group when the adoption was made. A snapshot. The current members come from the group's `cgroup.procs`. Missing means an empty list. |
| `created_at` | string | 2.0.0 | ISO 8601 UTC time, as for Binding. |
| `origins` | object: PID string to cgroup v2 path string | 2.2.0 | Each process's cgroup before it was moved, for example `"/user.slice/user-1000.slice/session-2.scope"`. Used to return the process to its own place on release. Missing or not an object means an empty object. Entries whose key is not all digits, or whose value is not a string, are dropped without a warning. |

## Validation on load

`state_from_dict` builds the in-memory model from the file. The rules below are the ones the loader
applies. A rejected entry is logged as a warning and left out. Loading does not change the file. The next save
by a writer rewrites the file from the loaded model, so rejected entries disappear from the file at that point.

Top level:

- The whole value must be a JSON object. Otherwise the file is handled as corrupt (see above).
- `version` is converted with `int()`; if that fails, it becomes `1`.
- `subnets`: an entry is kept only if its key passes the interface-name rule and its value is a canonical
  pool `/24` with third octet 1 to 254. Other entries are dropped.
- `ip_forward_prev`: any value is converted with `str()`. Null stays null.
- `dns`: each entry is checked with `dnsconf.check_servers`: a list of 1 to 4 IPv4 addresses that are usable
  as upstream resolvers (not loopback, link-local, multicast, unspecified or reserved). The address list is
  canonicalised and de-duplicated. The key is converted with `str()` and is not checked against the
  interface-name rule.
- `limits`: each entry must be an object. Its `down` and `up` values are checked with `shaping.check_mbit`:
  null, or a number from 0.064 to 100000 Mbit/s (booleans are rejected). An entry whose two values are both
  null is dropped. The key is converted with `str()` and is not checked against the interface-name rule.

Binding:

- The entry must be an object. Missing or empty (null or `""`) values of `iface`, `app`, `namespace`,
  `subnet`, `table` and `priority` make the entry invalid.
- `table` and `priority` are converted with `int()`, so a numeric string is accepted. Ranges are as in the
  table above.
- `subnet` must be a pool `/24`. `iface` and `namespace` must match their patterns.
- `pid`: a boolean, a value that cannot be converted, or a value of 1 or less becomes null.
- `uid`: accepted only if it is an integer of 0 or more and not a boolean. Otherwise it becomes null.
- `app` is not checked as an absolute path by the loader. The helper checks paths when it receives a request.

Adoption:

- The entry must be an object. Missing or empty values of `id`, `iface`, `app`, `slot`, `group`, `mark`,
  `table`, `priority` and `uid` make the entry invalid.
- `id` must match `^[0-9a-f]{8,32}$`.
- `slot`, `mark`, `table`, `priority` and `uid` are converted with `int()`. `uid` must not be a boolean.
- `pids` must be a list (or missing) whose items convert with `int()`. Anything else makes the entry invalid.
- `group` must equal `intermux/adopt-` plus `id` exactly. `slot` must be 1 to 200. `iface` must pass the
  interface-name rule. These checks exist because the helper uses these values in cgroup, sysctl and nftables
  paths.
- The loader does not range-check `mark`, `table`, `priority` or `uid`, and does not check `app`.

Consumers: a value read from this file must be checked again before it is used in a path, a command or a
rule. The helper does this for its own use.

## Compatibility

- Files from 2.0.0 and 2.1.x load on 2.2.0. Bindings have `uid` null. Adoptions have `origins` as an empty
  object. Files from before 2.1.0 have no `dns` or `limits` keys; those load as empty objects.
- Unknown keys are ignored when reading.
- The Python helper rewrites the whole file from its own model on every save. Keys that the Python model does
  not know are dropped on the next write. Only the Python helper and watcher may write the file, so this
  does not lose data today. A future writer must add its new fields to the Python model first.
- Adding a field: give it a default in `state.py`, read it with the same rules as above, and add a row to this
  document with the version that added it. Bump `version` only for a change that an old reader would
  misread, and say so in the changelog.
- Old files are kept in the fixtures under `tests/fixtures/` (planned for 3.0.0-beta.1), so both the Python
  and Rust loaders are tested against them.

## Example

A file with one binding and one adoption. It is in the exact form `save_state` writes. The binding is
on `wlan0`, which has a hand-chosen DNS and a speed limit. The adoption is for `sleep` (PID 192) on `wlan0`.

```json
{
  "adoptions": [
    {
      "app": "/usr/bin/sleep",
      "app_name": "sleep",
      "created_at": "2026-11-18T10:21:07+00:00",
      "group": "intermux/adopt-a87f9e9dfa6450ab",
      "id": "a87f9e9dfa6450ab",
      "iface": "wlan0",
      "mark": 261,
      "origins": {
        "192": "/user.slice/user-1000.slice/session-2.scope"
      },
      "pids": [
        192
      ],
      "priority": 2005,
      "slot": 5,
      "table": 2005,
      "uid": 1000
    }
  ],
  "bindings": [
    {
      "app": "/usr/bin/firefox",
      "app_name": "firefox",
      "created_at": "2026-11-18T10:15:42+00:00",
      "iface": "wlan0",
      "log_path": "/home/alice/.local/state/intermux/logs/firefox-wlan0.log",
      "namespace": "ns_1a2b3c4d",
      "pid": 4242,
      "priority": 1007,
      "profile_mode": "default",
      "subnet": "10.200.7.0/24",
      "table": 1007,
      "uid": 1000
    }
  ],
  "dns": {
    "wlan0": [
      "1.1.1.1",
      "9.9.9.9"
    ]
  },
  "ip_forward_prev": "0",
  "limits": {
    "wlan0": {
      "down": 20.0,
      "up": null
    }
  },
  "subnets": {
    "wlan0": "10.200.7.0/24"
  },
  "version": 1
}
```

## Rules for readers

- Do not write this file. To change anything, send a request to the root helper (through `pkexec` or
  `sudo`). The daemon never does this.
- Ignore unknown keys and unknown entries.
- Treat every value as data. Check it before you use it in a path, a command or a rule.
- A file that fails to parse is not a reason to write. Log it and show the empty state, or keep the last good
  state, as the reader's own design allows.
