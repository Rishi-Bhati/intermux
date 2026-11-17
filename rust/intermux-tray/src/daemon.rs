//! Client of intermuxd (docs/daemon-protocol.md): one connection, reconnected with backoff.
use intermux_common::{
    interface::Interface,
    protocol::MAX_LINE,
    state::{AdoptionView, BindingView},
    traffic::TrafficSample,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io,
    path::{Path, PathBuf},
    process::Stdio,
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::UnixStream,
    sync::{mpsc, oneshot},
};

const BACKOFF: [u64; 4] = [500, 1000, 2000, 5000];

#[derive(Debug)]
pub enum Update {
    Connected,
    Disconnected,
    Interfaces(Vec<Interface>),
    Bindings(Vec<BindingView>, Vec<AdoptionView>),
    Traffic(TrafficSample),
    Event(String, Value),
}

pub enum Command {
    /// `gui.raise`; answers `None` when the daemon cannot be asked.
    RaiseGui(oneshot::Sender<Option<bool>>),
}

pub fn socket_path() -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("INTERMUX_DAEMON_SOCKET").filter(|s| !s.is_empty()) {
        return Some(p.into());
    }
    let runtime = std::env::var_os("XDG_RUNTIME_DIR").filter(|s| !s.is_empty())?;
    Some(PathBuf::from(runtime).join("intermux").join("daemon.sock"))
}

fn daemon_binary() -> Option<PathBuf> {
    std::env::var_os("INTERMUX_DAEMON")
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .or_else(|| Some("/usr/lib/intermux/intermuxd".into()))
        .filter(|p| p.is_file())
}

/// Starts a detached program in a new session with stdio on /dev/null.
pub fn spawn_detached(program: &Path) -> io::Result<()> {
    let mut command = tokio::process::Command::new(program);
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // SAFETY: setsid is async-signal-safe and touches no memory of the parent.
    unsafe {
        command.pre_exec(|| nix::unistd::setsid().map(drop).map_err(io::Error::from));
    }
    // Tokio reaps the dropped child in the background.
    command.spawn().map(drop)
}

/// Like the Python `ensure_daemon`: start the daemon if nothing answers, then retry for 3 s.
pub async fn ensure(path: &Path) {
    if UnixStream::connect(path).await.is_ok() {
        return;
    }
    let Some(binary) = daemon_binary() else {
        log::warn!(
            "no daemon at {} and no daemon binary to start",
            path.display()
        );
        return;
    };
    log::info!("starting {}", binary.display());
    if let Err(e) = spawn_detached(&binary) {
        log::warn!("cannot start {}: {e}", binary.display());
        return;
    }
    for _ in 0..30 {
        tokio::time::sleep(Duration::from_millis(100)).await;
        if UnixStream::connect(path).await.is_ok() {
            return;
        }
    }
    log::warn!("the daemon did not come up within 3 s");
}

pub async fn run(
    path: PathBuf,
    updates: mpsc::Sender<Update>,
    mut commands: mpsc::Receiver<Command>,
) {
    let mut attempt = 0;
    loop {
        match session(&path, &updates, &mut commands).await {
            Ok(true) => attempt = 0,
            Ok(false) => (),
            Err(e) => log::debug!("daemon connection: {e}"),
        }
        if updates.send(Update::Disconnected).await.is_err() {
            return;
        }
        let delay = Duration::from_millis(BACKOFF[attempt.min(BACKOFF.len() - 1)]);
        attempt += 1;
        // Answer commands while waiting, so "Open InterMux" still works.
        let sleep = tokio::time::sleep(delay);
        tokio::pin!(sleep);
        loop {
            tokio::select! {
                _ = &mut sleep => break,
                cmd = commands.recv() => match cmd {
                    Some(Command::RaiseGui(reply)) => { let _ = reply.send(None); }
                    None => return,
                },
            }
        }
    }
}

enum Pending {
    Internal(&'static str),
    Raise(oneshot::Sender<Option<bool>>),
}

#[derive(Default)]
struct Requests {
    next_id: u64,
    pending: HashMap<u64, Pending>,
}
impl Requests {
    fn add(&mut self, method: &str, params: Value, purpose: Pending) -> String {
        self.next_id += 1;
        self.pending.insert(self.next_id, purpose);
        let mut line =
            json!({"v": 1, "id": self.next_id, "method": method, "params": params}).to_string();
        line.push('\n');
        line
    }
}

/// One connection. `Ok(true)` once `hello` succeeded.
async fn session(
    path: &Path,
    updates: &mpsc::Sender<Update>,
    commands: &mut mpsc::Receiver<Command>,
) -> io::Result<bool> {
    let stream = UnixStream::connect(path).await?;
    let (read, mut write) = stream.into_split();
    let mut reader = BufReader::new(read);
    let mut requests = Requests::default();
    let hello = requests.add(
        "hello",
        json!({"client": "tray", "protocols": [1]}),
        Pending::Internal("hello"),
    );
    write.write_all(hello.as_bytes()).await?;
    let mut hello_done = false;
    let mut line = String::new();
    loop {
        let mut out = String::new();
        tokio::select! {
            read = reader.read_line(&mut line) => {
                if read? == 0 {
                    return Ok(hello_done);
                }
                if line.len() > MAX_LINE {
                    return Err(io::Error::new(io::ErrorKind::InvalidData, "line exceeds 64 KiB"));
                }
                let message: Value = serde_json::from_str(&line)
                    .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
                line.clear();
                if let Some(event) = message["event"].as_str() {
                    let data = message["data"].clone();
                    match event {
                        "daemon.shutdown" => return Ok(hello_done),
                        "traffic.sample" => {
                            if let Ok(t) = serde_json::from_value(data) {
                                let _ = updates.send(Update::Traffic(t)).await;
                            }
                        }
                        _ => {
                            if event.starts_with("interface.") || event.starts_with("route.") {
                                out += &requests.add("interfaces.list", json!({}), Pending::Internal("interfaces"));
                            } else if event.starts_with("binding.") || event.starts_with("adoption.") || event == "app.exited" {
                                out += &requests.add("bindings.list", json!({}), Pending::Internal("bindings"));
                            }
                            let _ = updates.send(Update::Event(event.into(), data)).await;
                        }
                    }
                } else if let Some(id) = message["id"].as_u64() {
                    let error = message.get("error").filter(|e| !e.is_null());
                    let result = &message["result"];
                    match requests.pending.remove(&id) {
                        Some(Pending::Raise(reply)) => {
                            let _ = reply.send(error.is_none().then(|| result["raised"] == true));
                        }
                        Some(Pending::Internal(what)) => {
                            if let Some(e) = error {
                                if what == "hello" {
                                    return Err(io::Error::other(format!("hello refused: {e}")));
                                }
                                log::warn!("daemon {what}: {e}");
                                continue;
                            }
                            match what {
                                "hello" => {
                                    hello_done = true;
                                    log::info!("connected to intermuxd {}", result["daemon_version"]);
                                    let _ = updates.send(Update::Connected).await;
                                    out += &requests.add("subscribe", json!({"topics": ["interfaces", "bindings", "traffic"]}), Pending::Internal("subscribe"));
                                    out += &requests.add("interfaces.list", json!({}), Pending::Internal("interfaces"));
                                    out += &requests.add("bindings.list", json!({}), Pending::Internal("bindings"));
                                    out += &requests.add("traffic.snapshot", json!({}), Pending::Internal("traffic"));
                                }
                                "interfaces" => {
                                    if let Ok(list) = serde_json::from_value(result["interfaces"].clone()) {
                                        let _ = updates.send(Update::Interfaces(list)).await;
                                    }
                                }
                                "bindings" => {
                                    let b = serde_json::from_value(result["bindings"].clone());
                                    let a = serde_json::from_value(result["adoptions"].clone());
                                    if let (Ok(b), Ok(a)) = (b, a) {
                                        let _ = updates.send(Update::Bindings(b, a)).await;
                                    }
                                }
                                "traffic" => {
                                    if let Ok(t) = serde_json::from_value(result.clone()) {
                                        let _ = updates.send(Update::Traffic(t)).await;
                                    }
                                }
                                _ => (),
                            }
                        }
                        None => (),
                    }
                }
            }
            cmd = commands.recv() => match cmd {
                Some(Command::RaiseGui(reply)) if hello_done => {
                    out += &requests.add("gui.raise", json!({}), Pending::Raise(reply));
                }
                Some(Command::RaiseGui(reply)) => { let _ = reply.send(None); }
                None => return Ok(hello_done),
            },
        }
        if !out.is_empty() {
            write.write_all(out.as_bytes()).await?;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::net::UnixListener;

    #[tokio::test]
    async fn handshake_lists_and_events() {
        let dir = std::env::temp_dir().join(format!("intermux-tray-d-{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let sock = dir.join("d.sock");
        let _ = std::fs::remove_file(&sock);
        let listener = UnixListener::bind(&sock).unwrap();
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.unwrap();
            let (r, mut w) = stream.into_split();
            let mut lines = BufReader::new(r).lines();
            let mut methods = vec![];
            while let Some(l) = lines.next_line().await.unwrap() {
                let req: Value = serde_json::from_str(&l).unwrap();
                let method = req["method"].as_str().unwrap().to_string();
                let result = match method.as_str() {
                    "hello" => json!({"daemon_version": "x", "protocol": 1}),
                    "subscribe" => {
                        assert_eq!(
                            req["params"]["topics"],
                            json!(["interfaces", "bindings", "traffic"])
                        );
                        json!({"subscribed": []})
                    }
                    "interfaces.list" => json!({"interfaces": []}),
                    "bindings.list" => json!({"bindings": [], "adoptions": []}),
                    "traffic.snapshot" => json!({"t": 0, "interfaces": {}, "namespaces": {}}),
                    _ => json!({}),
                };
                let reply = json!({"v": 1, "id": req["id"], "result": result});
                w.write_all(format!("{reply}\n").as_bytes()).await.unwrap();
                methods.push(method);
                if methods.len() == 5 {
                    let ev = json!({"v": 1, "event": "route.lost", "data": {"interface": "wg0"}});
                    w.write_all(format!("{ev}\n").as_bytes()).await.unwrap();
                }
                if methods.len() == 6 {
                    w.write_all(b"{\"v\":1,\"event\":\"daemon.shutdown\",\"data\":{}}\n")
                        .await
                        .unwrap();
                    return methods;
                }
            }
            methods
        });
        let (tx, mut rx) = mpsc::channel(32);
        let (_ctx, mut crx) = mpsc::channel(1);
        assert!(session(&sock, &tx, &mut crx).await.unwrap());
        let methods = server.await.unwrap();
        assert_eq!(
            methods,
            [
                "hello",
                "subscribe",
                "interfaces.list",
                "bindings.list",
                "traffic.snapshot",
                "interfaces.list"
            ]
        );
        drop(tx);
        let mut got = vec![];
        while let Some(u) = rx.recv().await {
            got.push(format!("{u:?}").split('(').next().unwrap().to_string());
        }
        assert!(got.starts_with(&["Connected".into()]));
        assert!(got.contains(&"Event".to_string()));
        assert!(got.contains(&"Bindings".to_string()));
        std::fs::remove_dir_all(dir).unwrap();
    }
}
