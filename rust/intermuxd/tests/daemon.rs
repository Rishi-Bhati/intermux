use nix::{
    sys::signal::{kill, Signal},
    unistd::Pid,
};
use serde_json::{json, Value};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    os::unix::{
        fs::{symlink, PermissionsExt},
        net::UnixStream,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::atomic::{AtomicUsize, Ordering},
    thread,
    time::{Duration, Instant},
};

static SEQUENCE: AtomicUsize = AtomicUsize::new(0);
fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}
fn copy_tree(source: &Path, target: &Path) {
    fs::create_dir_all(target).unwrap();
    for entry in fs::read_dir(source).unwrap() {
        let entry = entry.unwrap();
        let dest = target.join(entry.file_name());
        let kind = entry.file_type().unwrap();
        if kind.is_dir() {
            copy_tree(&entry.path(), &dest);
        } else if kind.is_symlink() {
            symlink(fs::read_link(entry.path()).unwrap(), dest).unwrap();
        } else {
            fs::copy(entry.path(), dest).unwrap();
        }
    }
}

struct Daemon {
    child: Child,
    dir: PathBuf,
    socket: PathBuf,
}
impl Daemon {
    fn start() -> Self {
        Self::start_mode(false, false)
    }
    fn start_mode(activated: bool, missing_ip: bool) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "intermuxd-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&dir).unwrap();
        fs::create_dir(dir.join("proc")).unwrap();
        copy_tree(
            &fixtures().join("sys-class-net"),
            &dir.join("sys-class-net"),
        );
        fs::copy(
            fixtures().join("state-current.json"),
            dir.join("state.json"),
        )
        .unwrap();
        fs::create_dir(dir.join("bin")).unwrap();
        if !missing_ip {
            let script = "#!/bin/sh\ncase \"$2\" in\nlink) exec /usr/bin/cat \"$INTERMUX_TEST_FIXTURES/ip-link.json\" ;;\naddr) exec /usr/bin/cat \"$INTERMUX_TEST_FIXTURES/ip-addr.json\" ;;\nroute) exec /usr/bin/cat \"$INTERMUX_TEST_FIXTURES/ip-route.json\" ;;\nesac\nexit 1\n";
            fs::write(dir.join("bin/ip"), script).unwrap();
            fs::set_permissions(dir.join("bin/ip"), fs::Permissions::from_mode(0o755)).unwrap();
        }
        let mut command = if activated {
            let mut c = Command::new("/usr/bin/python3");
            // Python supplies a real inherited listener without adding unsafe Rust test code.
            c.args(["-c", "import os,socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[2]); s.listen(); os.dup2(s.fileno(),3); os.set_inheritable(3,True); os.environ['LISTEN_FDS']='1'; os.environ['LISTEN_PID']=str(os.getpid()); os.execv(sys.argv[1],[sys.argv[1]]+sys.argv[3:])"])
                .arg(env!("CARGO_BIN_EXE_intermuxd")).arg(dir.join("d.sock"));
            c
        } else {
            Command::new(env!("CARGO_BIN_EXE_intermuxd"))
        };
        command
            .arg("--socket")
            .arg(dir.join("d.sock"))
            .arg("--state")
            .arg(dir.join("state.json"))
            .arg("--sys-class-net")
            .arg(dir.join("sys-class-net"))
            .arg("--proc")
            .arg(dir.join("proc"))
            .env("PATH", dir.join("bin"))
            .env("INTERMUX_TEST_FIXTURES", fixtures())
            .env("XDG_RUNTIME_DIR", &dir)
            .env_remove("LISTEN_FDS")
            .env_remove("LISTEN_PID")
            .stdout(Stdio::null())
            .stderr(Stdio::from(fs::File::create(dir.join("stderr")).unwrap()));
        let child = command.spawn().unwrap();
        let socket = dir.join("d.sock");
        Self { child, dir, socket }
    }
    fn connect(&mut self) -> Peer {
        let start = Instant::now();
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                panic!(
                    "daemon exited {status}: {}",
                    fs::read_to_string(self.dir.join("stderr")).unwrap()
                );
            }
            if let Ok(stream) = UnixStream::connect(&self.socket) {
                stream
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                stream
                    .set_write_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                return Peer {
                    reader: BufReader::new(stream),
                    id: 0,
                };
            }
            assert!(
                start.elapsed() < Duration::from_secs(3),
                "socket did not appear"
            );
            thread::sleep(Duration::from_millis(10));
        }
    }
    fn replace(&self, value: &Value) {
        fs::write(
            self.dir.join("state.new"),
            serde_json::to_vec(value).unwrap(),
        )
        .unwrap();
        fs::rename(self.dir.join("state.new"), self.dir.join("state.json")).unwrap();
    }
    fn restart(&mut self, default_socket: bool) {
        self.child.kill().ok();
        self.child.wait().unwrap();
        let mut command = Command::new(env!("CARGO_BIN_EXE_intermuxd"));
        self.socket = if default_socket {
            self.dir.join("intermux/daemon.sock")
        } else {
            self.dir.join("d.sock")
        };
        if !default_socket {
            command.arg("--socket").arg(&self.socket);
        }
        command
            .arg("--state")
            .arg(self.dir.join("state.json"))
            .arg("--sys-class-net")
            .arg(self.dir.join("sys-class-net"))
            .arg("--proc")
            .arg(self.dir.join("proc"))
            .env("XDG_RUNTIME_DIR", &self.dir)
            .env("PATH", self.dir.join("bin"))
            .env("INTERMUX_TEST_FIXTURES", fixtures())
            .env_remove("LISTEN_FDS")
            .env_remove("LISTEN_PID")
            .stdout(Stdio::null())
            .stderr(Stdio::from(
                fs::File::create(self.dir.join("stderr")).unwrap(),
            ));
        self.child = command.spawn().unwrap();
    }
    fn terminate(&mut self) {
        kill(Pid::from_raw(self.child.id() as i32), Signal::SIGTERM).unwrap();
    }
    fn wait_success(&mut self) {
        let start = Instant::now();
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                assert!(status.success(), "{status}");
                return;
            }
            assert!(start.elapsed() < Duration::from_secs(3));
            thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for Daemon {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_dir_all(&self.dir);
    }
}
struct Peer {
    reader: BufReader<UnixStream>,
    id: u64,
}
impl Peer {
    fn send(&mut self, method: &str, params: Value) -> u64 {
        self.id += 1;
        self.raw(&json!({"v":1,"id":self.id,"method":method,"params":params}).to_string());
        self.id
    }
    fn raw(&mut self, text: &str) {
        let mut bytes = text.as_bytes().to_vec();
        bytes.push(b'\n');
        self.reader.get_mut().write_all(&bytes).unwrap();
    }
    fn message(&mut self) -> Value {
        let mut line = String::new();
        assert!(
            self.reader.read_line(&mut line).unwrap() > 0,
            "unexpected disconnect"
        );
        let v: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(v["v"], 1);
        v
    }
    fn request(&mut self, method: &str, params: Value) -> Value {
        let id = self.send(method, params);
        loop {
            let v = self.message();
            if v["id"] == id {
                return v;
            }
        }
    }
    fn hello(&mut self) {
        assert_eq!(
            self.request("hello", json!({"client":"cli","protocols":[1]}))["result"],
            json!({"daemon_version":"3.0.0-beta.1","protocol":1,"capabilities":["interfaces","bindings","traffic","gui"]})
        );
    }
    fn event(&mut self, name: &str) -> Value {
        loop {
            let v = self.message();
            if v["event"] == name {
                assert!(v.get("id").is_none());
                return v;
            }
        }
    }
}

#[test]
fn protocol_state_notifications_and_shutdown() {
    let mut daemon = Daemon::start();
    let mut peer = daemon.connect();
    assert_eq!(
        peer.request("ping", json!({}))["error"]["code"],
        "bad_request"
    );
    assert_eq!(
        peer.request("hello", json!({"client":"cli","protocols":[2]}))["error"]["code"],
        "unsupported_version"
    );
    assert_eq!(
        peer.request("ping", json!({}))["error"]["code"],
        "bad_request"
    );
    peer.hello();
    assert_eq!(
        peer.request("traffic.snapshot", json!({}))["result"],
        json!({"t":0,"interfaces":{},"namespaces":{}})
    );
    assert_eq!(
        peer.request(
            "subscribe",
            json!({"topics":["bindings","interfaces","traffic","unknown","bindings"]})
        )["result"],
        json!({"subscribed":["bindings","interfaces","traffic"]})
    );
    let listed = peer.request("bindings.list", json!({}));
    assert_eq!(listed["result"]["bindings"].as_array().unwrap().len(), 1);
    assert_eq!(listed["result"]["bindings"][0]["uid"], 1000);
    assert_eq!(listed["result"]["bindings"][0]["running"], false);
    assert_eq!(listed["result"]["bindings"][0]["pids"], json!([]));
    let names = peer.request("interfaces.list", json!({}))["result"]["interfaces"]
        .as_array()
        .unwrap()
        .clone();
    assert!(names.iter().any(|v| v["name"] == "wlan0"
        && v["dns"] == json!(["1.1.1.1", "9.9.9.9"])
        && v["limit"] == json!({"down":20.0,"up":null})));
    assert!(!names
        .iter()
        .any(|v| v["name"] == "lo" || v["kind"] == "virtual" || v["name"] == "eth1"));
    let mut state: Value =
        serde_json::from_slice(&fs::read(daemon.dir.join("state.json")).unwrap()).unwrap();
    let mut b = state["bindings"][0].clone();
    b["app"] = json!("/usr/bin/sleep");
    b["app_name"] = json!("sleep");
    state["bindings"].as_array_mut().unwrap().push(b);
    let start = Instant::now();
    daemon.replace(&state);
    assert_eq!(peer.event("binding.started")["data"]["app_name"], "sleep");
    assert!(start.elapsed() < Duration::from_secs(2));
    assert_eq!(
        peer.request("ping", json!({}))["result"],
        json!({"pong":true})
    );
    assert_eq!(
        peer.request("foo", json!({}))["error"],
        json!({"code":"unknown_method","message":"no method named foo"})
    );
    assert_eq!(
        peer.request("subscribe", json!({"topics":true}))["error"]["code"],
        "bad_request"
    );
    peer.raw("invalid JSON");
    assert_eq!(peer.message()["error"]["code"], "bad_request");
    peer.reader.get_mut().write_all(b"\xff\n").unwrap();
    assert_eq!(peer.message()["error"]["code"], "bad_request");
    let mut boundary = json!({"v":1,"id":999,"method":"ping","params":{}}).to_string();
    boundary.push_str(&" ".repeat(64 * 1024 - boundary.len()));
    peer.raw(&boundary);
    assert_eq!(peer.message()["result"], json!({"pong":true}));
    let mut second = daemon.connect();
    second.hello();
    let sample = peer.event("traffic.sample");
    assert!(sample["data"]["t"].as_f64().unwrap() > 0.0);
    assert!(sample["data"]["interfaces"].get("eth0").is_some());
    assert_eq!(
        peer.request("gui.raise", json!({}))["result"],
        json!({"raised":false})
    );
    second.request("subscribe", json!({"topics":["gui"]}));
    assert_eq!(
        peer.request("gui.raise", json!({}))["result"],
        json!({"raised":true})
    );
    assert_eq!(second.event("gui.raise")["data"], json!({}));
    let activation = json!({"activation_token":"test-window-activation"});
    assert_eq!(
        peer.request("gui.raise", activation.clone())["result"],
        json!({"raised":true})
    );
    assert_eq!(second.event("gui.raise")["data"], activation);
    assert_eq!(
        peer.request("gui.raise", json!({"activation_token":false}))["error"]["code"],
        "bad_request"
    );
    let mut oversized = daemon.connect();
    oversized.hello();
    oversized.raw(&"x".repeat(70 * 1024));
    let mut byte = [0];
    match oversized.reader.read(&mut byte) {
        Ok(0) => (),
        Err(e) if e.kind() == std::io::ErrorKind::ConnectionReset => (),
        other => panic!("oversized connection remained open: {other:?}"),
    }
    daemon.terminate();
    assert_eq!(peer.event("daemon.shutdown")["data"], json!({}));
    assert_eq!(second.event("daemon.shutdown")["data"], json!({}));
    daemon.wait_success();
    assert!(!daemon.dir.join("d.sock").exists());
}

#[test]
fn invalid_state_retains_last_good_view_and_recovers() {
    let mut daemon = Daemon::start();
    let mut peer = daemon.connect();
    peer.hello();
    peer.request("subscribe", json!({"topics":["bindings"]}));
    for text in ["{\"bindings\": [", "[]"] {
        fs::write(daemon.dir.join("state.new"), text).unwrap();
        fs::rename(daemon.dir.join("state.new"), daemon.dir.join("state.json")).unwrap();
        thread::sleep(Duration::from_millis(350));
        assert_eq!(
            peer.request("bindings.list", json!({}))["result"]["bindings"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }
    fs::remove_file(daemon.dir.join("state.json")).unwrap();
    thread::sleep(Duration::from_millis(350));
    assert_eq!(
        peer.request("bindings.list", json!({}))["result"]["bindings"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    daemon.replace(&json!({}));
    assert_eq!(peer.event("binding.stopped")["data"]["app_name"], "firefox");
    assert_eq!(
        peer.request("bindings.list", json!({}))["result"]["bindings"],
        json!([])
    );
    let stderr = fs::read_to_string(daemon.dir.join("stderr")).unwrap();
    assert_eq!(stderr.matches("retaining last good state").count(), 1);
}

#[test]
fn slow_client_does_not_block_other_clients() {
    let mut daemon = Daemon::start();
    let mut slow = daemon.connect();
    slow.hello();
    let mut healthy = daemon.connect();
    healthy.hello();
    let bytes = (0..1000)
        .map(|id| {
            format!("{{\"v\":1,\"id\":{id},\"method\":\"interfaces.list\",\"params\":{{}}}}\n")
        })
        .collect::<String>();
    let _ = slow.reader.get_mut().write_all(bytes.as_bytes());
    let start = Instant::now();
    assert_eq!(
        healthy.request("ping", json!({}))["result"],
        json!({"pong":true})
    );
    assert!(start.elapsed() < Duration::from_secs(1));
    thread::sleep(Duration::from_millis(250));
    let mut discarded = Vec::new();
    match slow.reader.read_to_end(&mut discarded) {
        Ok(_) => (),
        Err(e) if e.kind() == std::io::ErrorKind::ConnectionReset => (),
        other => panic!("slow client was not dropped: {other:?}"),
    }
    assert_eq!(
        healthy.request("ping", json!({}))["result"],
        json!({"pong":true})
    );
}

#[test]
fn activation_preserves_socket_and_missing_ip_keeps_running() {
    let mut daemon = Daemon::start_mode(true, true);
    let mut peer = daemon.connect();
    peer.hello();
    let interfaces = peer.request("interfaces.list", json!({}));
    assert_eq!(interfaces["result"]["interfaces"], json!([]));
    daemon.terminate();
    peer.event("daemon.shutdown");
    daemon.wait_success();
    assert!(daemon.dir.join("d.sock").exists());
    assert!(fs::read_to_string(daemon.dir.join("stderr"))
        .unwrap()
        .contains("cannot read interfaces using ip"));
}

#[test]
fn cli_and_socket_modes() {
    let version = Command::new(env!("CARGO_BIN_EXE_intermuxd"))
        .arg("--version")
        .env_remove("XDG_RUNTIME_DIR")
        .output()
        .unwrap();
    assert!(version.status.success());
    assert_eq!(version.stdout, b"intermuxd 3.0.0-beta.1\n");
    let missing = Command::new(env!("CARGO_BIN_EXE_intermuxd"))
        .env_remove("XDG_RUNTIME_DIR")
        .env_remove("LISTEN_FDS")
        .env_remove("LISTEN_PID")
        .output()
        .unwrap();
    assert!(!missing.status.success());
    assert!(String::from_utf8(missing.stderr)
        .unwrap()
        .contains("XDG_RUNTIME_DIR is unset"));
    let mut daemon = Daemon::start();
    let mut peer = daemon.connect();
    peer.hello();
    assert_eq!(
        fs::metadata(daemon.dir.join("d.sock"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    let conflict = Command::new(env!("CARGO_BIN_EXE_intermuxd"))
        .arg("--socket")
        .arg(daemon.dir.join("d.sock"))
        .env("XDG_RUNTIME_DIR", &daemon.dir)
        .output()
        .unwrap();
    assert!(!conflict.status.success());
    assert_eq!(
        peer.request("ping", json!({}))["result"],
        json!({"pong":true})
    );
}

#[test]
fn stale_socket_recovery_and_default_runtime_directory() {
    let mut daemon = Daemon::start();
    let mut peer = daemon.connect();
    peer.hello();
    daemon.restart(false); // SIGKILL leaves a stale socket, which the new daemon probes and removes.
    peer = daemon.connect();
    peer.hello();
    daemon.terminate();
    peer.event("daemon.shutdown");
    daemon.wait_success();
    assert!(!daemon.socket.exists());
    daemon.restart(true);
    peer = daemon.connect();
    peer.hello();
    assert_eq!(
        fs::metadata(daemon.dir.join("intermux"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    assert_eq!(
        fs::metadata(&daemon.socket).unwrap().permissions().mode() & 0o777,
        0o600
    );
    kill(Pid::from_raw(daemon.child.id() as i32), Signal::SIGINT).unwrap();
    peer.event("daemon.shutdown");
    daemon.wait_success();
    assert!(!daemon.socket.exists());
}
