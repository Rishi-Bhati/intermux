use crate::model::{Request, State};
use rune::AppHandle;
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const SNAPSHOT_INTERVAL: Duration = Duration::from_secs(1);
const RECONNECT_INTERVAL: Duration = Duration::from_secs(2);
const MAX_REQUEST: usize = 512 * 1024;
const MAX_REPLY: usize = 4 * 1024 * 1024;

#[derive(Deserialize)]
pub(crate) struct Failure {
    message: String,
}

#[derive(Deserialize)]
#[serde(untagged)]
pub(crate) enum Message {
    Failure { v: u32, id: u64, error: Failure },
    Reply { v: u32, id: u64, result: Value },
    Event { v: u32, event: String, data: Value },
}

pub(crate) enum Input {
    Request(Request),
    Message(u64, Message),
    Closed(u64),
    Stop,
}

struct Connection {
    generation: u64,
    child: Child,
    stdin: ChildStdin,
    reader: JoinHandle<()>,
    pending: BTreeMap<u64, Request>,
}

fn bridge_command() -> Command {
    if let Some(binary) = std::env::var_os("INTERMUX_GUI_BRIDGE") {
        let mut command = Command::new(binary);
        command.arg("gui-bridge");
        return command;
    }
    if let Ok(binary) = std::env::current_exe() {
        let adjacent = binary.with_file_name("intermux");
        if adjacent.is_file() {
            let mut command = Command::new(adjacent);
            command.arg("gui-bridge");
            return command;
        }
    }
    let python = std::env::var_os("INTERMUX_GUI_BRIDGE_PYTHON").unwrap_or_else(|| "python3".into());
    let source = std::env::var_os("INTERMUX_GUI_BRIDGE_CWD")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."));
    let mut command = Command::new(python);
    command
        .current_dir(source)
        .args(["-m", "intermux", "gui-bridge"]);
    command
}

impl Connection {
    fn start(generation: u64, input: Sender<Input>) -> io::Result<Self> {
        let mut child = bridge_command()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?;
        let stdin = child.stdin.take().expect("piped stdin");
        let stdout = child.stdout.take().expect("piped stdout");
        let reader = thread::spawn(move || {
            read_replies(BufReader::new(stdout), |message| {
                input.send(Input::Message(generation, message)).is_ok()
            });
            let _ = input.send(Input::Closed(generation));
        });
        Ok(Self {
            generation,
            child,
            stdin,
            reader,
            pending: BTreeMap::new(),
        })
    }

    fn send(&mut self, request: Request, handle: &AppHandle<State>) -> bool {
        let mut line = json!({
            "v": 1, "id": request.id, "op": request.op, "args": request.args,
        })
        .to_string()
        .into_bytes();
        line.push(b'\n');
        if line.len() > MAX_REQUEST {
            deliver(
                handle,
                request,
                Err("InterMux could not send a request of this size.".into()),
            );
            return true;
        }
        self.pending.insert(request.id, request);
        self.stdin.write_all(&line).is_ok()
    }

    fn receive(&mut self, message: Message, handle: &AppHandle<State>) {
        match message {
            Message::Reply { v: 1, id, result } => {
                if let Some(request) = self.pending.remove(&id) {
                    deliver(handle, request, Ok(result));
                }
            }
            Message::Failure { v: 1, id, error } => {
                if let Some(request) = self.pending.remove(&id) {
                    deliver(handle, request, Err(error.message));
                }
            }
            Message::Event { v: 1, event, data } if event == "gui.raise" => {
                if let Some(token) = data["activation_token"].as_str().filter(|s| !s.is_empty()) {
                    handle.activate(token);
                } else {
                    handle.open();
                }
            }
            Message::Event { v: 1, event, .. } if event == "refresh" => refresh(handle),
            _ => {}
        }
    }

    fn close(self, handle: &AppHandle<State>, report_failure: bool) {
        let Connection {
            mut child,
            stdin,
            reader,
            pending,
            ..
        } = self;
        drop(stdin);
        let deadline = Instant::now() + Duration::from_millis(350);
        loop {
            if matches!(child.try_wait(), Ok(Some(_))) {
                break;
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                break;
            }
            thread::sleep(Duration::from_millis(25));
        }
        let _ = reader.join();
        if report_failure {
            for request in pending.into_values() {
                let message = if request.op == "snapshot" || request.op == "apps" {
                    "The desktop service disconnected. Reconnecting…"
                } else {
                    "The desktop service disconnected. Check current routes before retrying."
                };
                deliver(handle, request, Err(message.into()));
            }
        }
    }
}

fn read_replies(mut reader: impl BufRead, mut send: impl FnMut(Message) -> bool) {
    let mut line = Vec::new();
    loop {
        line.clear();
        let result = reader
            .by_ref()
            .take(MAX_REPLY as u64 + 1)
            .read_until(b'\n', &mut line);
        if !matches!(result, Ok(1..)) || line.len() > MAX_REPLY || !line.ends_with(b"\n") {
            break;
        }
        match serde_json::from_slice(&line) {
            Ok(message) => {
                if !send(message) {
                    break;
                }
            }
            Err(error) => {
                eprintln!("InterMux received an invalid desktop-service reply: {error}");
                break;
            }
        }
    }
}

fn deliver(handle: &AppHandle<State>, request: Request, result: Result<Value, String>) {
    handle.update(move |state, _| state.receive(request, result));
}

fn refresh(handle: &AppHandle<State>) {
    handle.update_if(|state, _| {
        state.request("snapshot", json!({}));
        false
    });
}

pub struct Bridge {
    input: Sender<Input>,
    worker: Option<JoinHandle<()>>,
}

impl Bridge {
    pub fn start(handle: AppHandle<State>) -> Self {
        let (input, events) = mpsc::channel();
        let sender = input.clone();
        handle.update(|state, _| {
            state.attach(sender);
            state.request("snapshot", json!({}));
            state.request("apps", json!({}));
        });
        let sender = input.clone();
        let worker = thread::spawn(move || run(handle, sender, events));
        Self {
            input,
            worker: Some(worker),
        }
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        let _ = self.input.send(Input::Stop);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

fn run(handle: AppHandle<State>, input: Sender<Input>, events: Receiver<Input>) {
    let mut connection: Option<Connection> = None;
    let mut generation = 0;
    let mut retry = Instant::now();
    let mut next_snapshot = retry + SNAPSHOT_INTERVAL;
    let mut was_connected = false;
    loop {
        let now = Instant::now();
        if now >= next_snapshot {
            refresh(&handle);
            next_snapshot = now + SNAPSHOT_INTERVAL;
        }
        match events.recv_timeout(next_snapshot.saturating_duration_since(now)) {
            Ok(Input::Message(id, message)) => {
                if let Some(active) = connection.as_mut().filter(|c| c.generation == id) {
                    active.receive(message, &handle);
                }
            }
            Ok(Input::Closed(id)) => {
                if connection.as_ref().is_some_and(|c| c.generation == id) {
                    handle.update(|state, _| {
                        state.snapshot_error =
                            Some("The desktop service disconnected. Reconnecting…".into());
                    });
                    connection.take().unwrap().close(&handle, true);
                    retry = Instant::now() + RECONNECT_INTERVAL;
                }
            }
            Ok(Input::Request(request)) => {
                if connection.is_none() && Instant::now() >= retry {
                    generation += 1;
                    match Connection::start(generation, input.clone()) {
                        Ok(active) => {
                            if was_connected {
                                handle.update_if(|state, _| {
                                    state.request("apps", json!({}));
                                    false
                                });
                            }
                            was_connected = true;
                            connection = Some(active);
                        }
                        Err(error) => {
                            retry = Instant::now() + RECONNECT_INTERVAL;
                            deliver(
                                &handle,
                                request,
                                Err(format!("Could not start the desktop service: {error}")),
                            );
                            continue;
                        }
                    }
                }
                if let Some(active) = connection.as_mut() {
                    if !active.send(request, &handle) {
                        connection.take().unwrap().close(&handle, true);
                        retry = Instant::now() + RECONNECT_INTERVAL;
                    }
                } else {
                    deliver(
                        &handle,
                        request,
                        Err("The desktop service is reconnecting.".into()),
                    );
                }
            }
            Ok(Input::Stop) | Err(RecvTimeoutError::Disconnected) => break,
            Err(RecvTimeoutError::Timeout) => {}
        }
    }
    if let Some(active) = connection {
        active.close(&handle, false);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn handles_reply_error_events_and_null_results_on_the_same_pipe() {
        let source: &[u8] = b"{\"v\":1,\"id\":2,\"result\":null}\n{\"v\":1,\"id\":3,\"error\":{\"code\":\"denied\",\"message\":\"Denied\"}}\n{\"v\":1,\"event\":\"gui.raise\",\"data\":{\"activation_token\":\"test\"}}\n";
        let (send, replies) = mpsc::channel();
        read_replies(source, |message| send.send(message).is_ok());
        assert!(matches!(
            replies.recv().unwrap(),
            Message::Reply {
                v: 1,
                id: 2,
                result: Value::Null
            }
        ));
        assert!(
            matches!(replies.recv().unwrap(), Message::Failure { v: 1, id: 3, error } if error.message == "Denied")
        );
        assert!(
            matches!(replies.recv().unwrap(), Message::Event { v: 1, event, data } if event == "gui.raise" && data["activation_token"] == "test")
        );
    }
}
