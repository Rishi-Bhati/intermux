use crate::{
    config::Config,
    events, socket,
    transport::{self, Client, Command},
    view::{self, Traffic, View},
    watch::{self, Change},
};
use intermux_common::{
    interface::Link,
    protocol::{
        ErrorCode, Event, HelloParams, Request, Response, SubscribeParams, DAEMON_VERSION, TOPICS,
        VERSION,
    },
    state::{load_state, State},
    traffic::TrafficSample,
};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    io,
    path::Path,
    time::Duration,
};
use tokio::{
    signal::unix::{signal, SignalKind},
    sync::mpsc,
    task::JoinSet,
    time::{Instant, MissedTickBehavior},
};

#[derive(Default)]
struct Server {
    clients: BTreeMap<u64, Client>,
    state: State,
    links: Vec<Link>,
    view: View,
    lost_routes: BTreeSet<String>,
    traffic: Traffic,
    last_sample: TrafficSample,
    state_error: bool,
    ip_error: bool,
}
impl Server {
    fn send(&mut self, client: u64, value: Value) {
        if self.clients.get(&client).is_some_and(|c| !c.send(&value)) {
            self.drop_client(client);
        }
    }
    fn drop_client(&mut self, id: u64) {
        if let Some(client) = self.clients.remove(&id) {
            client.cancel.notify_one();
        }
    }
    fn broadcast(&mut self, topic: Option<&str>, event: Event) {
        let recipients: Vec<u64> = self
            .clients
            .iter()
            .filter(|(_, c)| topic.is_none_or(|t| c.subscribed(t)))
            .map(|(&id, _)| id)
            .collect();
        let value = json!(event);
        for id in recipients {
            self.send(id, value.clone());
        }
    }
    fn request(&mut self, client: u64, bytes: &[u8]) {
        if !self.clients.contains_key(&client) {
            return;
        }
        let parsed: Value = match serde_json::from_slice(bytes) {
            Ok(v) => v,
            Err(_) => {
                self.send(
                    client,
                    json!(Response::error(
                        Value::Null,
                        ErrorCode::BadRequest,
                        "expected a UTF-8 JSON request"
                    )),
                );
                return;
            }
        };
        let id = parsed.get("id").cloned().unwrap_or(Value::Null);
        if parsed.get("id").is_none() || parsed.get("params").is_none() {
            self.send(
                client,
                json!(Response::error(
                    id,
                    ErrorCode::BadRequest,
                    "expected v, id, method and object params"
                )),
            );
            return;
        }
        let request: Request = match serde_json::from_value::<Request>(parsed) {
            Ok(r) if r.params.is_object() => r,
            _ => {
                self.send(
                    client,
                    json!(Response::error(
                        id,
                        ErrorCode::BadRequest,
                        "expected v, id, method and object params"
                    )),
                );
                return;
            }
        };
        let result = self.dispatch(client, request);
        self.send(client, json!(result));
    }
    fn dispatch(&mut self, client: u64, r: Request) -> Response {
        let error = |code, message| Response::error(r.id.clone(), code, message);
        if r.v != VERSION {
            return error(
                ErrorCode::UnsupportedVersion,
                "unsupported protocol version".into(),
            );
        }
        if !self.clients[&client].hello && r.method != "hello" {
            return error(
                ErrorCode::BadRequest,
                "hello must be the first request".into(),
            );
        }
        let result = match r.method.as_str() {
            "hello" => {
                let params: HelloParams = match serde_json::from_value(r.params) {
                    Ok(p) => p,
                    Err(_) => {
                        return error(
                            ErrorCode::BadRequest,
                            "hello needs client and protocols".into(),
                        )
                    }
                };
                if !params.protocols.contains(&VERSION) {
                    return error(
                        ErrorCode::UnsupportedVersion,
                        "no supported protocol in protocols".into(),
                    );
                }
                self.clients.get_mut(&client).unwrap().hello = true;
                json!({"daemon_version": DAEMON_VERSION, "protocol": VERSION, "capabilities": TOPICS})
            }
            "subscribe" => {
                let params: SubscribeParams = match serde_json::from_value(r.params) {
                    Ok(p) => p,
                    Err(_) => return error(ErrorCode::BadRequest, "subscribe needs topics".into()),
                };
                let mut topics = vec![];
                for topic in params.topics {
                    if TOPICS.contains(&topic.as_str()) && !topics.contains(&topic) {
                        topics.push(topic);
                    }
                }
                self.clients.get_mut(&client).unwrap().topics = topics.clone();
                json!({"subscribed": topics})
            }
            "interfaces.list" => json!({"interfaces": self.view.interfaces}),
            "bindings.list" => {
                json!({"bindings": self.view.bindings, "adoptions": self.view.adoptions})
            }
            "traffic.snapshot" if self.last_sample.t == 0.0 => {
                json!({"t":0,"interfaces":{},"namespaces":{}})
            }
            "traffic.snapshot" => json!(self.last_sample),
            "gui.raise" => {
                let raised = self.clients.values().any(|c| c.subscribed("gui"));
                if raised {
                    self.broadcast(Some("gui"), Event::new("gui.raise", json!({})));
                }
                json!({"raised": raised})
            }
            "ping" => json!({"pong": true}),
            other => return error(ErrorCode::UnknownMethod, format!("no method named {other}")),
        };
        Response::success(r.id, result)
    }
    async fn reload_state(&mut self, config: &Config) -> io::Result<()> {
        let path = config.state.clone();
        match tokio::task::spawn_blocking(move || load_state(&path))
            .await
            .map_err(io::Error::other)?
        {
            Ok(state) => {
                self.state = state;
                self.state_error = false;
            }
            Err(e) => {
                if !self.state_error {
                    log::warn!(
                        "cannot load {}: {e}; retaining last good state",
                        config.state.display()
                    );
                }
                self.state_error = true;
            }
        }
        Ok(())
    }
    async fn reload_links(&mut self, config: &Config) {
        match view::read_links(&config.sys_class_net).await {
            Ok(links) => {
                self.links = links;
                self.ip_error = false;
            }
            Err(e) => {
                if !self.ip_error {
                    log::warn!("cannot read interfaces using ip: {e}; no interfaces listed");
                }
                self.ip_error = true;
                self.links.clear();
            }
        }
    }
    async fn refresh(&mut self, config: &Config, emit: bool) -> io::Result<()> {
        let state = self.state.clone();
        let links = self.links.clone();
        let config = config.clone();
        let mut next = tokio::task::spawn_blocking(move || {
            view::build(
                &state,
                &links,
                &config,
                Path::new("/var/run/netns"),
                Path::new("/sys/fs/cgroup"),
            )
        })
        .await
        .map_err(io::Error::other)?;
        if self.ip_error {
            next.interfaces.clear();
        }
        if emit {
            for (topic, event) in events::changes(&self.view, &next, &mut self.lost_routes) {
                self.broadcast(Some(topic), event);
            }
        }
        self.view = next;
        Ok(())
    }
}

pub async fn run(config: Config) -> io::Result<()> {
    let mut terminate = signal(SignalKind::terminate())?;
    let mut interrupt = signal(SignalKind::interrupt())?;
    let (listener, _socket_guard) = socket::listen(&config).await?;
    let (command_tx, mut commands) = mpsc::channel(128);
    let (change_tx, mut changes) = mpsc::channel(32);
    let mut netlink_fallback = match watch::netlink(change_tx.clone()) {
        Ok(()) => false,
        Err(e) => {
            log::warn!("netlink notifications unavailable: {e}; rescanning every 5 s");
            true
        }
    };
    let mut state_fallback = match watch::state(&config.state, change_tx.clone()) {
        Ok(()) => false,
        Err(e) => {
            log::warn!("state notifications unavailable: {e}; polling every 2 s");
            true
        }
    };
    let mut server = Server::default();
    server.reload_state(&config).await?;
    server.reload_links(&config).await;
    server.refresh(&config, false).await?;
    let mut clients = JoinSet::new();
    let mut next_id = 0;
    let mut liveness = tokio::time::interval_at(
        Instant::now() + Duration::from_secs(2),
        Duration::from_secs(2),
    );
    let mut traffic = tokio::time::interval_at(
        Instant::now() + Duration::from_secs(1),
        Duration::from_secs(1),
    );
    let mut fallback = tokio::time::interval_at(
        Instant::now() + Duration::from_secs(5),
        Duration::from_secs(5),
    );
    for interval in [&mut liveness, &mut traffic, &mut fallback] {
        interval.set_missed_tick_behavior(MissedTickBehavior::Skip);
    }
    let mut interface_deadline = None;
    let mut state_deadline = None;
    log::info!("listening on {}", config.socket.display());
    loop {
        tokio::select! {
            _ = terminate.recv() => break,
            _ = interrupt.recv() => break,
            accepted = listener.accept() => {
                let (stream,_) = accepted?;
                match socket::same_user(&stream) {
                    Ok(true) => {
                        next_id += 1;
                        let (client, output) = transport::client();
                        clients.spawn(transport::serve(next_id, stream, output, client.cancel.clone(), command_tx.clone()));
                        server.clients.insert(next_id, client);
                    }
                    Ok(false) => log::warn!("refusing peer with a different uid"),
                    Err(e) => log::warn!("cannot verify peer credentials: {e}"),
                }
            }
            Some(command) = commands.recv() => match command {
                Command::Request(id,bytes) => server.request(id,&bytes),
                Command::Disconnected(id) => server.drop_client(id),
            },
            Some(_) = clients.join_next(), if !clients.is_empty() => (),
            Some(change) = changes.recv() => match change {
                Change::Interfaces => interface_deadline = Some(Instant::now() + Duration::from_millis(200)),
                Change::State => state_deadline = Some(Instant::now() + Duration::from_millis(200)),
                Change::NetlinkFailed => netlink_fallback = true,
                Change::StateWatchFailed => state_fallback = true,
            },
            _ = deadline(interface_deadline) => {
                interface_deadline = None;
                server.reload_links(&config).await;
                server.refresh(&config, true).await?;
            }
            _ = deadline(state_deadline) => {
                state_deadline = None;
                server.reload_state(&config).await?;
                server.refresh(&config, true).await?;
            }
            _ = fallback.tick(), if netlink_fallback => {
                server.reload_links(&config).await;
                server.refresh(&config, true).await?;
            }
            _ = liveness.tick() => {
                // Retry a missing or replaced watch without creating or modifying the state directory.
                if state_fallback {
                    server.reload_state(&config).await?;
                    if watch::state(&config.state, change_tx.clone()).is_ok() { state_fallback = false; }
                }
                server.refresh(&config, true).await?;
            }
            _ = traffic.tick() => {
                if server.clients.values().any(|c| c.subscribed("traffic")) {
                    let base = config.sys_class_net.clone(); let view = server.view.clone(); let state = server.state.clone();
                    let mut tracker = std::mem::take(&mut server.traffic);
                    let (tracker, sample) = tokio::task::spawn_blocking(move || {
                        let sample = tracker.sample(&view,&state,&base); (tracker,sample)
                    }).await.map_err(io::Error::other)?;
                    server.traffic = tracker; server.last_sample = sample;
                    server.broadcast(Some("traffic"), Event::new("traffic.sample", json!(server.last_sample)));
                } else { server.traffic = Traffic::default(); }
            }
        }
    }
    server.broadcast(None, Event::new("daemon.shutdown", json!({})));
    // Dropping the senders lets writers drain their queues and finish; cap the drain for stalled peers.
    server.clients.clear();
    drop(commands);
    let _ = tokio::time::timeout(Duration::from_secs(1), async {
        while clients.join_next().await.is_some() {}
    })
    .await;
    clients.abort_all();
    Ok(())
}

async fn deadline(time: Option<Instant>) {
    match time {
        Some(time) => tokio::time::sleep_until(time).await,
        None => std::future::pending().await,
    }
}
