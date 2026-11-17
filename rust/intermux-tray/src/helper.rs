//! Root helper calls: `pkexec <helper> helper`, one JSON request on stdin, one JSON response on stdout.
use crate::model::Action;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    io::{self, Read, Write},
    path::PathBuf,
    process::{Command, Stdio},
    sync::mpsc,
    time::Duration,
};

pub const DEFAULT_HELPER: &str = "/usr/lib/intermux/intermux";
pub const TIMEOUT: Duration = Duration::from_secs(120);
/// Same set as `intermux/config.py: ENV_ALLOWLIST`.
pub const ENV_ALLOWLIST: [&str; 13] = [
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "PULSE_SERVER",
    "PIPEWIRE_REMOTE",
    "LANG",
    "LC_ALL",
    "GTK_THEME",
    "QT_QPA_PLATFORM",
];

pub fn filter_env(vars: impl IntoIterator<Item = (String, String)>) -> BTreeMap<String, String> {
    vars.into_iter()
        .filter(|(k, _)| ENV_ALLOWLIST.contains(&k.as_str()))
        .collect()
}

/// The helper request for an action, or `None` for actions the tray handles itself.
pub fn request(action: &Action, env: &BTreeMap<String, String>) -> Option<Value> {
    let (name, params) = match action {
        Action::Stop { iface, app, .. } => ("stop_app", json!({"iface": iface, "app": app})),
        Action::Restart { iface, app, .. } => (
            "restart_app",
            json!({"iface": iface, "app": app, "env": env}),
        ),
        Action::Release { id, .. } => ("release_adoption", json!({"id": id})),
        Action::SetLimit { iface, down, up } => {
            ("set_limit", json!({"iface": iface, "down": down, "up": up}))
        }
        Action::OpenGui | Action::ToggleAutostart | Action::Quit => return None,
    };
    Some(json!({"v": 1, "action": name, "params": params}))
}

/// The interface an action changes (only one call per interface at a time).
pub fn busy_key(action: &Action) -> Option<&str> {
    match action {
        Action::Stop { iface, .. }
        | Action::Restart { iface, .. }
        | Action::Release { iface, .. }
        | Action::SetLimit { iface, .. } => Some(iface),
        _ => None,
    }
}

/// Notification summary when an action fails.
pub fn failure_summary(action: &Action) -> String {
    match action {
        Action::Stop { name, .. } => format!("InterMux: could not stop {name}"),
        Action::Restart { name, .. } => format!("InterMux: could not restart {name}"),
        Action::Release { name, .. } => format!("InterMux: could not release {name}"),
        Action::SetLimit { iface, .. } => {
            format!("InterMux: could not set the speed limit on {iface}")
        }
        _ => "InterMux: the action failed".into(),
    }
}

pub fn argv() -> Vec<String> {
    let path = std::env::var_os("INTERMUX_HELPER")
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| DEFAULT_HELPER.into());
    vec![
        "pkexec".into(),
        path.to_string_lossy().into_owned(),
        "helper".into(),
    ]
}

#[derive(Debug, Clone, PartialEq)]
pub struct Output {
    pub code: Option<i32>,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

/// Runs one request through `spawn(argv, stdin)` and returns the helper's `data` or its error message.
pub fn call(
    request: &Value,
    spawn: impl FnOnce(&[String], &[u8]) -> io::Result<Output>,
) -> Result<Value, String> {
    let mut input = serde_json::to_vec(request).expect("JSON Value serializes");
    input.push(b'\n');
    let output = spawn(&argv(), &input).map_err(|e| format!("cannot run the helper: {e}"))?;
    parse(&output)
}

pub fn parse(output: &Output) -> Result<Value, String> {
    if matches!(output.code, Some(126 | 127)) {
        return Err("Authorization was dismissed or denied.".into());
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let response = text.lines().rev().find_map(|line| {
        serde_json::from_str::<Value>(line.trim())
            .ok()
            .filter(Value::is_object)
    });
    match response {
        Some(r) if r["ok"] == true => Ok(r.get("data").cloned().unwrap_or(Value::Null)),
        Some(r) => Err(r["error"]["message"]
            .as_str()
            .unwrap_or("the helper reported an error")
            .to_string()),
        None => {
            let stderr = String::from_utf8_lossy(&output.stderr);
            let stderr = stderr.trim();
            Err(if stderr.is_empty() {
                match output.code {
                    Some(code) => format!("the helper exited with code {code}"),
                    None => "the helper was killed".into(),
                }
            } else {
                stderr.lines().last().unwrap_or(stderr).to_string()
            })
        }
    }
}

/// Spawns the real process with the helper timeout. Blocking: call it off the UI thread.
pub fn spawn(argv: &[String], input: &[u8]) -> io::Result<Output> {
    let mut child = Command::new(&argv[0])
        .args(&argv[1..])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let pid = nix::unistd::Pid::from_raw(child.id() as i32);
    let mut stdin = child.stdin.take().expect("piped stdin");
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let input = input.to_vec();
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut err = Vec::new();
        let reader = std::thread::spawn(move || {
            let _ = stderr.read_to_end(&mut err);
            err
        });
        let _ = stdin.write_all(&input);
        drop(stdin);
        let mut out = Vec::new();
        let _ = stdout.read_to_end(&mut out);
        let err = reader.join().unwrap_or_default();
        let status = child.wait();
        let _ = tx.send(status.map(|s| Output {
            code: s.code(),
            stdout: out,
            stderr: err,
        }));
    });
    match rx.recv_timeout(TIMEOUT) {
        Ok(result) => result,
        Err(_) => {
            let _ = nix::sys::signal::kill(pid, nix::sys::signal::Signal::SIGKILL);
            Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "the helper did not answer within 120 s",
            ))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env() -> BTreeMap<String, String> {
        BTreeMap::from([("DISPLAY".to_string(), ":0".to_string())])
    }
    fn ok(stdout: &str, code: i32) -> Output {
        Output {
            code: Some(code),
            stdout: stdout.into(),
            stderr: vec![],
        }
    }

    #[test]
    fn env_allowlist() {
        let vars = [
            ("DISPLAY", ":0"),
            ("WAYLAND_DISPLAY", "wayland-0"),
            ("LANG", "C.UTF-8"),
            ("QT_QPA_PLATFORM", "wayland"),
            ("PATH", "/usr/bin"),
            ("LD_PRELOAD", "/evil.so"),
            ("HOME", "/home/u"),
            ("display", "x"),
        ]
        .map(|(k, v)| (k.to_string(), v.to_string()));
        let kept = filter_env(vars);
        assert_eq!(
            kept.keys().collect::<Vec<_>>(),
            ["DISPLAY", "LANG", "QT_QPA_PLATFORM", "WAYLAND_DISPLAY"]
        );
        assert_eq!(ENV_ALLOWLIST.len(), 13);
    }

    #[test]
    fn requests_match_helper_schema() {
        let (iface, app, name) = (
            "wlan0".to_string(),
            "/usr/bin/firefox".to_string(),
            "firefox".to_string(),
        );
        let stop = Action::Stop {
            iface: iface.clone(),
            app: app.clone(),
            name: name.clone(),
        };
        assert_eq!(
            request(&stop, &env()).unwrap(),
            json!({"v": 1, "action": "stop_app", "params": {"iface": "wlan0", "app": "/usr/bin/firefox"}})
        );
        let restart = Action::Restart {
            iface: iface.clone(),
            app,
            name,
        };
        assert_eq!(
            request(&restart, &env()).unwrap(),
            json!({"v": 1, "action": "restart_app",
                   "params": {"iface": "wlan0", "app": "/usr/bin/firefox", "env": {"DISPLAY": ":0"}}})
        );
        let release = Action::Release {
            iface: iface.clone(),
            id: "a87f9e9dfa6450ab".into(),
            name: "sleep".into(),
        };
        assert_eq!(
            request(&release, &env()).unwrap(),
            json!({"v": 1, "action": "release_adoption", "params": {"id": "a87f9e9dfa6450ab"}})
        );
        let limit = Action::SetLimit {
            iface: iface.clone(),
            down: Some(25.0),
            up: Some(25.0),
        };
        assert_eq!(
            request(&limit, &env()).unwrap(),
            json!({"v": 1, "action": "set_limit", "params": {"iface": "wlan0", "down": 25.0, "up": 25.0}})
        );
        let none = Action::SetLimit {
            iface,
            down: None,
            up: None,
        };
        assert_eq!(
            request(&none, &env()).unwrap().to_string(),
            r#"{"action":"set_limit","params":{"down":null,"iface":"wlan0","up":null},"v":1}"#
        );
        assert_eq!(request(&Action::Quit, &env()), None);
        assert_eq!(busy_key(&none), Some("wlan0"));
        assert_eq!(busy_key(&Action::OpenGui), None);
        assert_eq!(failure_summary(&stop), "InterMux: could not stop firefox");
    }

    #[test]
    fn call_uses_pkexec_and_stdin() {
        let req = json!({"v": 1, "action": "stop_app", "params": {}});
        let result = call(&req, |argv, input| {
            assert_eq!(argv[0], "pkexec");
            assert_eq!(argv[2], "helper");
            assert_eq!(input, format!("{req}\n").as_bytes());
            Ok(ok(r#"{"ok":true,"data":{"stopped":1}}"#, 0))
        });
        assert_eq!(result, Ok(json!({"stopped": 1})));
        let failed = call(&req, |_, _| {
            Err(io::Error::new(io::ErrorKind::NotFound, "no pkexec"))
        });
        assert_eq!(failed, Err("cannot run the helper: no pkexec".into()));
    }

    #[test]
    fn responses() {
        assert_eq!(parse(&ok(r#"{"ok":true}"#, 0)), Ok(Value::Null));
        assert_eq!(
            parse(&ok(
                r#"{"ok":false,"error":{"code":"not_found","message":"no such app"}}"#,
                1
            )),
            Err("no such app".into())
        );
        assert_eq!(
            parse(&ok("", 126)),
            Err("Authorization was dismissed or denied.".into())
        );
        assert_eq!(
            parse(&ok("", 127)),
            Err("Authorization was dismissed or denied.".into())
        );
        assert_eq!(
            parse(&ok("", 3)),
            Err("the helper exited with code 3".into())
        );
        let crashed = Output {
            code: Some(1),
            stdout: b"garbage".to_vec(),
            stderr: b"Traceback\nValueError: x\n".to_vec(),
        };
        assert_eq!(parse(&crashed), Err("ValueError: x".into()));
    }

    #[test]
    fn real_spawn_round_trip() {
        let argv = ["sh", "-c", "cat; exit 4"].map(String::from);
        let out = spawn(&argv, b"{\"ok\":true}\n").unwrap();
        assert_eq!(
            out,
            Output {
                code: Some(4),
                stdout: b"{\"ok\":true}\n".to_vec(),
                stderr: vec![]
            }
        );
    }
}
