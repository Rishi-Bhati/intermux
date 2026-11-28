use crate::bridge::Input;
use rune::{Image, ImageCache, TextEdit, Waker};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::{BTreeMap, VecDeque};
use std::net::Ipv4Addr;
use std::sync::mpsc::Sender;
use std::time::{Duration, Instant};

#[derive(Debug)]
pub struct Request {
    pub id: u64,
    pub op: String,
    pub args: Value,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Rate {
    pub down: Option<f64>,
    pub up: Option<f64>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Interface {
    pub name: String,
    pub kind: String,
    pub state: String,
    pub bindable: bool,
    pub ipv4: Vec<String>,
    pub gateway: Option<String>,
    pub dns: Vec<String>,
    pub limit: Option<Rate>,
    pub rates: Option<Rate>,
    pub namespace_rates: Option<Rate>,
    pub has_namespace: bool,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Binding {
    pub iface: String,
    pub app: String,
    pub name: String,
    pub kind: String,
    pub id: Value,
    pub running: bool,
    pub pids: Vec<u32>,
    pub log_path: Option<String>,
}

impl Binding {
    pub fn key(&self) -> String {
        format!("app:{}:{}:{}", self.iface, self.app, self.id)
    }
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Snapshot {
    pub interfaces: Vec<Interface>,
    pub bindings: Vec<Binding>,
    pub passwordless: bool,
    pub dependencies: Vec<String>,
    pub daemon_connected: bool,
    pub vpn_available: bool,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct App {
    pub name: String,
    pub path: String,
    pub description: String,
    pub icon: Option<String>,
    pub bindable: bool,
    pub reason: Option<String>,
    pub recent: bool,
    pub recent_iface: Option<String>,
    pub recent_dns: Option<String>,
}

#[derive(Clone, Debug, Default, Deserialize)]
#[serde(default)]
pub struct Vpn {
    pub name: String,
    pub uuid: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub device: Option<String>,
    pub active: bool,
}

#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub enum Page {
    #[default]
    Routes,
    Vpn,
    Settings,
    Queue,
    Compose,
}

#[derive(Clone, PartialEq, Eq)]
pub enum Selection {
    Interface(String),
    App(String),
}

#[derive(Clone, Default)]
pub struct Composer {
    pub search: TextEdit,
    pub path: TextEdit,
    pub app: Option<App>,
    pub iface: String,
    pub interface_chosen: bool,
    pub manual: bool,
    pub advanced: bool,
    pub dns: TextEdit,
    pub down: TextEdit,
    pub up: TextEdit,
    pub policy: &'static str,
}

#[derive(Clone)]
pub struct Job {
    pub id: u64,
    pub app: App,
    pub iface: String,
    pub policy: String,
    pub dns: Value,
    pub limit: Value,
    pub issue: Option<String>,
    pub outcome: Option<String>,
}

struct LaunchOptions {
    iface: String,
    policy: &'static str,
    dns: Value,
    limit: Value,
}

impl Job {
    fn op(&self) -> Value {
        if self.policy == "shift" {
            return json!({"op": "adopt", "iface": self.iface, "app": self.app.path});
        }
        let mut op = json!({
            "op": "bind", "iface": self.iface, "app": self.app.path,
            "running_policy": self.policy, "dns": self.dns
        });
        if !self.limit.is_null() {
            op["limit"] = self.limit.clone();
        }
        op
    }
}

#[derive(Clone)]
pub enum Dialog {
    Confirm {
        title: &'static str,
        detail: String,
        label: &'static str,
        op: &'static str,
        args: Value,
    },
    Running {
        id: u64,
        count: u64,
    },
    Dns(String),
    Limit(String),
    Move(Binding),
    Import,
}

#[derive(Default)]
pub struct State {
    tx: Option<Sender<Input>>,
    next_id: u64,
    pending: BTreeMap<u64, String>,
    pub demo: bool,
    pub narrow: bool,
    pub logo: Option<Image>,
    pub icons: BTreeMap<String, Image>,
    image_cache: Option<ImageCache>,
    pub reset_kill_apps: bool,
    pub page: Page,
    pub snapshot: Option<Snapshot>,
    pub snapshot_error: Option<String>,
    force_snapshot: bool,
    pub apps: Vec<App>,
    pub apps_loaded: bool,
    pub vpns: Vec<Vpn>,
    pub vpn_loaded: bool,
    pub apps_only: bool,
    pub selection: Option<Selection>,
    pub composer: Composer,
    pub dialog: Option<Dialog>,
    pub field: TextEdit,
    pub field2: TextEdit,
    pub form_error: Option<String>,
    pub status: Option<(String, bool)>,
    pub queue: Vec<Job>,
    pub histories: BTreeMap<String, VecDeque<f64>>,
    pub public_ips: BTreeMap<String, String>,
    verified_at: BTreeMap<String, Instant>,
    pub report: Option<String>,
    draft: Option<(bool, LaunchOptions)>,
    batch_check: Vec<u64>,
    batch_submit: Vec<u64>,
    running: VecDeque<(u64, u64)>,
}

impl State {
    pub fn set_up_images(&mut self, waker: Waker) {
        let cache = ImageCache::new(waker);
        let logo = cache.svg(
            &include_bytes!("../../../website/public/brand/mark.svg")[..],
            30.0,
            2.0,
        );
        if self.demo {
            let _ = logo.wait();
            crate::ui::load_demo_icons(self, &cache);
        }
        self.logo = Some(logo);
        self.image_cache = Some(cache);
    }

    pub fn attach(&mut self, tx: Sender<Input>) {
        self.tx = Some(tx);
        self.apps_only = true;
    }

    fn id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }

    pub fn request(&mut self, op: &str, args: Value) {
        if self.pending.values().any(|value| value == op) {
            if op == "snapshot" && args["force"] == true {
                self.force_snapshot = true;
            }
            return;
        }
        if self.demo {
            let result = match op {
                "running_apps" => Ok(json!({})),
                "batch" => Ok(Value::Array(
                    args["ops"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .map(|_| json!({"ok":true,"data":{}}))
                        .collect(),
                )),
                "snapshot" | "apps" | "vpn_list" => return,
                _ => {
                    self.status = Some((
                        "Sample data. This action requires a live connection.".into(),
                        false,
                    ));
                    return;
                }
            };
            let id = self.id();
            self.receive(
                Request {
                    id,
                    op: op.into(),
                    args,
                },
                result,
            );
            return;
        }
        let id = self.id();
        let request = Request {
            id,
            op: op.into(),
            args,
        };
        self.pending.insert(id, op.into());
        if let Some(Err(error)) = self.tx.as_ref().map(|tx| tx.send(Input::Request(request))) {
            let Input::Request(request) = error.0 else {
                unreachable!()
            };
            self.receive(
                request,
                Err("The desktop bridge has disconnected. Reopen InterMux to reconnect.".into()),
            );
        } else if self.tx.is_none() {
            self.pending.remove(&id);
        }
    }

    pub fn busy(&self) -> Option<&str> {
        self.pending
            .values()
            .find(|op| !matches!(op.as_str(), "snapshot" | "apps" | "vpn_list"))
            .map(String::as_str)
    }

    pub fn change_page(&mut self, page: Page) {
        self.page = page;
        if page == Page::Vpn {
            self.request("vpn_list", json!({}));
        }
    }

    pub fn compose(&mut self, iface: Option<String>) {
        let interface_chosen = iface.is_some();
        self.composer = Composer {
            iface: iface.unwrap_or_else(|| {
                self.interfaces()
                    .iter()
                    .find(|iface| iface.bindable && iface.state == "up")
                    .map(|iface| iface.name.clone())
                    .unwrap_or_default()
            }),
            policy: "ask",
            interface_chosen,
            ..Composer::default()
        };
        self.form_error = None;
        self.page = Page::Compose;
        if !self.apps_loaded {
            self.request("apps", json!({}));
        }
    }

    pub fn choose_app(&mut self, app: App) {
        if let Some(iface) = &app.recent_iface
            && !self.composer.interface_chosen
            && self
                .interfaces()
                .iter()
                .any(|item| item.bindable && &item.name == iface)
        {
            self.composer.iface.clone_from(iface);
        }
        if self.composer.dns.text.is_empty()
            && let Some(dns) = &app.recent_dns
        {
            self.composer.dns.text.clone_from(dns);
        }
        self.composer.app = Some(app);
        self.composer.manual = false;
        self.form_error = None;
    }

    pub fn interfaces(&self) -> &[Interface] {
        self.snapshot.as_ref().map_or(&[], |s| &s.interfaces)
    }

    pub fn bindings(&self) -> &[Binding] {
        self.snapshot.as_ref().map_or(&[], |s| &s.bindings)
    }

    pub fn submit(&mut self, now: bool) {
        if self.busy().is_some() {
            return;
        }
        self.form_error = None;
        if !self
            .interfaces()
            .iter()
            .any(|iface| iface.name == self.composer.iface && iface.bindable && iface.state == "up")
        {
            self.form_error = Some("Choose an available interface.".into());
            return;
        }
        let dns = match parse_dns(&self.composer.dns.text, "keep") {
            Ok(dns) => dns,
            Err(error) => {
                self.form_error = Some(error);
                return;
            }
        };
        let limit = match parse_limit(&self.composer.down.text, &self.composer.up.text) {
            Ok(limit) => limit,
            Err(error) => {
                self.form_error = Some(error);
                return;
            }
        };
        let options = LaunchOptions {
            iface: self.composer.iface.clone(),
            policy: self.composer.policy,
            dns,
            limit,
        };
        if self.composer.manual {
            let path = self.composer.path.text.trim().to_string();
            if path.is_empty() {
                self.form_error = Some("Enter an executable path or command name.".into());
                return;
            }
            if self.demo {
                let name = path.rsplit('/').next().unwrap_or(&path).to_string();
                self.add_job(
                    App {
                        name,
                        path,
                        bindable: true,
                        ..App::default()
                    },
                    now,
                    options,
                );
                return;
            }
            self.draft = Some((now, options));
            self.request("resolve_app", json!({"path": path}));
        } else if let Some(app) = self.composer.app.clone().filter(|a| a.bindable) {
            self.add_job(app, now, options);
        } else {
            self.form_error = Some("Choose an app.".into());
        }
    }

    fn add_job(&mut self, app: App, now: bool, options: LaunchOptions) {
        if !app.bindable {
            self.form_error = Some(
                app.reason
                    .unwrap_or_else(|| "This app cannot be routed.".into()),
            );
            return;
        }
        if let Some(existing) = self
            .queue
            .iter()
            .find(|j| j.app.path == app.path && j.iface == options.iface && j.outcome.is_none())
        {
            let id = existing.id;
            self.page = Page::Queue;
            if now {
                self.start(vec![id]);
            }
            return;
        }
        if self
            .queue
            .iter()
            .filter(|job| job.outcome.is_none())
            .count()
            >= 50
        {
            self.form_error = Some("Start or remove queued apps before adding more. A batch can contain up to 50 apps.".into());
            return;
        }
        let id = self.id();
        self.queue.push(Job {
            id,
            app,
            iface: options.iface,
            dns: options.dns,
            limit: options.limit,
            policy: options.policy.into(),
            issue: None,
            outcome: None,
        });
        self.page = Page::Queue;
        if now {
            self.start(vec![id]);
        }
    }

    pub fn start(&mut self, ids: Vec<u64>) {
        if self.busy().is_some() || ids.is_empty() || !self.batch_check.is_empty() {
            return;
        }
        self.batch_check = ids;
        let paths: Vec<_> = self
            .queue
            .iter()
            .filter(|job| self.batch_check.contains(&job.id))
            .filter(|job| job.policy == "ask")
            .map(|job| &job.app.path)
            .collect();
        self.request("running_apps", json!({"paths": paths}));
    }

    pub fn running_choice(&mut self, id: u64, policy: Option<&str>) {
        if let Some(job) = self.queue.iter_mut().find(|job| job.id == id) {
            if let Some(policy) = policy {
                job.policy = policy.to_string();
            } else {
                job.issue =
                    Some("Skipped. Start again to choose how to handle the running app.".into());
                self.batch_check.retain(|candidate| *candidate != id);
            }
        }
        self.next_running();
    }

    fn next_running(&mut self) {
        self.dialog = None;
        if let Some((id, count)) = self.running.pop_front() {
            self.dialog = Some(Dialog::Running { id, count });
            return;
        }
        self.batch_submit = std::mem::take(&mut self.batch_check);
        if self.batch_submit.is_empty() {
            return;
        }
        let mut ops = Vec::new();
        for id in &self.batch_submit {
            if let Some(job) = self.queue.iter_mut().find(|j| j.id == *id) {
                job.issue = None;
                ops.push(job.op());
            }
        }
        self.request("batch", json!({"ops": ops}));
    }

    pub fn receive(&mut self, request: Request, response: Result<Value, String>) {
        self.pending.remove(&request.id);
        if request.op == "snapshot" && std::mem::take(&mut self.force_snapshot) {
            self.request("snapshot", json!({"force": true}));
        }
        let result = match response {
            Ok(result) => result,
            Err(error) => {
                match request.op.as_str() {
                    "snapshot" => self.snapshot_error = Some(error),
                    "resolve_app" => {
                        self.draft = None;
                        self.form_error = Some(error);
                    }
                    "running_apps" => {
                        for job in &mut self.queue {
                            if self.batch_check.contains(&job.id) {
                                job.issue = Some(error.clone());
                            }
                        }
                        self.batch_check.clear();
                    }
                    "batch" => {
                        for job in &mut self.queue {
                            if self.batch_submit.contains(&job.id) {
                                job.issue = Some(error.clone());
                            }
                        }
                        self.batch_submit.clear();
                        self.request("snapshot", json!({}));
                    }
                    _ => self.status = Some((error, true)),
                }
                return;
            }
        };
        match request.op.as_str() {
            "snapshot" => match serde_json::from_value::<Snapshot>(result) {
                Ok(snapshot) => {
                    self.snapshot_error = None;
                    self.verified_at.retain(|iface, stamp| {
                        let fresh = stamp.elapsed() < Duration::from_secs(300)
                            && snapshot
                                .interfaces
                                .iter()
                                .any(|item| &item.name == iface && item.state == "up");
                        if !fresh {
                            self.public_ips.remove(iface);
                        }
                        fresh
                    });
                    for iface in &snapshot.interfaces {
                        if iface.state != "up" {
                            self.histories.remove(&iface.name);
                            self.public_ips.remove(&iface.name);
                        } else if let Some(down) = iface.rates.as_ref().and_then(|rate| rate.down) {
                            let points = self.histories.entry(iface.name.clone()).or_default();
                            if points.len() >= 42 {
                                points.pop_front();
                            }
                            points.push_back(down);
                        }
                    }
                    self.histories.retain(|name, _| {
                        snapshot.interfaces.iter().any(|iface| &iface.name == name)
                    });
                    let selection_exists = match &self.selection {
                        Some(Selection::Interface(name)) => {
                            snapshot.interfaces.iter().any(|iface| &iface.name == name)
                        }
                        Some(Selection::App(key)) => {
                            snapshot.bindings.iter().any(|app| &app.key() == key)
                        }
                        None => false,
                    };
                    if !selection_exists {
                        self.selection = snapshot
                            .interfaces
                            .iter()
                            .find(|iface| iface.state == "up")
                            .map(|iface| Selection::Interface(iface.name.clone()));
                    }
                    self.snapshot = Some(snapshot);
                }
                Err(error) => {
                    self.snapshot_error =
                        Some(format!("Could not read the connection state. {error}"))
                }
            },
            "apps" => match serde_json::from_value(result) {
                Ok(apps) => {
                    let apps: Vec<App> = apps;
                    if let Some(cache) = &self.image_cache {
                        self.icons = apps
                            .iter()
                            .filter_map(|app| {
                                let path = app.icon.as_ref()?;
                                Some((app.path.clone(), cache.file(path, 30.0, 2.0)))
                            })
                            .collect();
                    }
                    self.apps = apps;
                    self.apps_loaded = true;
                }
                Err(error) => {
                    self.status = Some((format!("Could not read installed apps. {error}"), true))
                }
            },
            "resolve_app" => {
                if let Some((now, options)) = self.draft.take() {
                    match serde_json::from_value(result) {
                        Ok(app) => self.add_job(app, now, options),
                        Err(error) => {
                            self.form_error =
                                Some(format!("Could not read the executable. {error}"))
                        }
                    }
                }
            }
            "running_apps" => {
                self.running = self
                    .batch_check
                    .iter()
                    .filter_map(|id| {
                        let job = self
                            .queue
                            .iter()
                            .find(|j| j.id == *id && j.policy == "ask")?;
                        let count = result[&job.app.path].as_u64().unwrap_or(0);
                        (count > 0).then_some((*id, count))
                    })
                    .collect();
                self.next_running();
            }
            "batch" => {
                let ids = std::mem::take(&mut self.batch_submit);
                for (index, id) in ids.iter().enumerate() {
                    let Some(job) = self.queue.iter_mut().find(|job| job.id == *id) else {
                        continue;
                    };
                    let entry = &result[index];
                    if entry["ok"].as_bool() == Some(true) {
                        let mut outcome = if job.policy == "shift" {
                            String::from("Session shifted")
                        } else {
                            String::from("Started")
                        };
                        if let Some(warnings) = entry["data"]["warnings"].as_array() {
                            for warning in warnings.iter().filter_map(Value::as_str) {
                                outcome.push_str(". ");
                                outcome.push_str(warning);
                            }
                        }
                        if job.policy == "shift" && (!job.limit.is_null() || job.dns != "keep") {
                            outcome.push_str(
                                ". DNS and bandwidth settings apply to newly launched apps.",
                            );
                        }
                        job.outcome = Some(outcome);
                    } else {
                        job.issue = Some(entry["error"]["message"].as_str()
                            .unwrap_or("InterMux did not return a result for this app. Check active routes before retrying.").into());
                        if matches!(
                            entry["error"]["code"].as_str(),
                            Some("running" | "already_running" | "app_running")
                        ) {
                            job.policy = "ask".into();
                        }
                    }
                }
                self.request("snapshot", json!({}));
                self.request("apps", json!({}));
            }
            "vpn_list" => match serde_json::from_value(result) {
                Ok(vpns) => {
                    self.vpns = vpns;
                    self.vpn_loaded = true;
                }
                Err(error) => {
                    self.status = Some((format!("Could not read VPN connections. {error}"), true))
                }
            },
            "verify" => {
                let iface = request.args["iface"].as_str().unwrap_or_default();
                if let Some(ip) = result["public_ip"].as_str() {
                    self.public_ips.insert(iface.into(), ip.into());
                    self.verified_at.insert(iface.into(), Instant::now());
                    self.status = Some((format!("{iface} public IP · {ip}"), false));
                } else {
                    self.public_ips.remove(iface);
                    self.verified_at.remove(iface);
                    self.status = Some((
                        format!(
                            "Could not check the public IP for {iface}. {}",
                            result["error"].as_str().unwrap_or_default()
                        ),
                        true,
                    ));
                }
            }
            "save_report" => {
                self.report = result["path"].as_str().map(str::to_string);
                self.status = self
                    .report
                    .as_ref()
                    .map(|path| (format!("Saved report to {path}"), false));
            }
            op => {
                let mut label = match op {
                    "stop_app" => "App stopped",
                    "restart_app" => "App restarted",
                    "move_app" => "App moved",
                    "kill_app" => "App force quit",
                    "release_adoption" => "Session returned to the default route",
                    "remove_namespace" => "Namespace removed",
                    "set_dns" => "DNS updated",
                    "set_limit" => "Bandwidth limit updated",
                    "passwordless" => "Authorization settings updated",
                    "vpn_connect" => "VPN connected",
                    "vpn_disconnect" => "VPN disconnected",
                    "vpn_import" => "VPN imported",
                    "teardown_idle" => "Idle cleanup finished",
                    "reconnect" => "Route reconnect finished",
                    "reset" => "Reset finished",
                    "open_log" => "Log opened",
                    "open_report" => "Report opened",
                    "open_report_folder" => "Report folder opened",
                    _ => "Operation finished",
                }
                .to_string();
                let mut warning = false;
                if let Some(pids) = result["still_running"]
                    .as_array()
                    .filter(|pids| !pids.is_empty())
                {
                    label = format!(
                        "Some processes are still running ({})",
                        pids.iter()
                            .map(Value::to_string)
                            .collect::<Vec<_>>()
                            .join(", ")
                    );
                    warning = true;
                } else if op == "remove_namespace" && result["removed"].as_bool() == Some(false) {
                    label = "The namespace was not removed.".into();
                    warning = true;
                } else if let Some(warnings) = result["warnings"].as_array() {
                    for note in warnings.iter().filter_map(Value::as_str) {
                        label.push_str(". ");
                        label.push_str(note);
                        warning = true;
                    }
                }
                self.status = Some((label, warning));
                self.request("snapshot", json!({}));
                if op.starts_with("vpn_") {
                    self.request("vpn_list", json!({}));
                }
            }
        }
    }

    pub fn choose_interface(&mut self, name: String) {
        self.selection = Some(Selection::Interface(name));
    }

    pub fn confirm(
        &mut self,
        title: &'static str,
        detail: String,
        label: &'static str,
        op: &'static str,
        args: Value,
    ) {
        self.dialog = Some(Dialog::Confirm {
            title,
            detail,
            label,
            op,
            args,
        });
    }

    pub fn edit_interface(&mut self, iface: Interface, dns: bool) {
        self.field = TextEdit::default();
        self.field2 = TextEdit::default();
        self.form_error = None;
        if dns {
            self.field.text = iface.dns.join(", ");
            self.dialog = Some(Dialog::Dns(iface.name));
        } else {
            if let Some(limit) = iface.limit {
                self.field.text = limit.down.map(|v| v.to_string()).unwrap_or_default();
                self.field2.text = limit.up.map(|v| v.to_string()).unwrap_or_default();
            }
            self.dialog = Some(Dialog::Limit(iface.name));
        }
    }

    pub fn apply_interface(&mut self, iface: String, dns: bool) {
        if dns {
            match parse_dns(&self.field.text, "auto") {
                Ok(dns) => {
                    self.dialog = None;
                    self.request("set_dns", json!({"iface": iface, "dns": dns}));
                }
                Err(error) => self.form_error = Some(error),
            }
        } else {
            match parse_limit(&self.field.text, &self.field2.text) {
                Ok(limit) => {
                    self.dialog = None;
                    self.request(
                        "set_limit",
                        json!({"iface": iface, "down": limit["down"], "up": limit["up"]}),
                    );
                }
                Err(error) => self.form_error = Some(error),
            }
        }
    }
}

fn parse_dns(s: &str, default: &str) -> Result<Value, String> {
    if s.trim().is_empty() {
        return Ok(json!(default));
    }
    let name = s.trim().to_ascii_lowercase();
    if name == "automatic" {
        return Ok(json!("auto"));
    }
    if matches!(name.as_str(), "auto" | "cloudflare" | "google" | "quad9") {
        return Ok(json!(name));
    }
    let addresses: Vec<_> = s
        .split(|ch: char| ch == ',' || ch.is_whitespace())
        .filter(|part| !part.is_empty())
        .collect();
    if addresses.is_empty()
        || addresses
            .iter()
            .any(|part| part.parse::<Ipv4Addr>().is_err())
    {
        return Err("Enter IPv4 addresses separated by commas, or use auto.".into());
    }
    Ok(json!(addresses))
}

fn parse_limit(down: &str, up: &str) -> Result<Value, String> {
    fn rate(s: &str) -> Result<Option<f64>, String> {
        if s.trim().is_empty() {
            return Ok(None);
        }
        let value = s
            .trim()
            .parse::<f64>()
            .ok()
            .filter(|v| v.is_finite() && *v >= 0.0)
            .ok_or_else(|| {
                "Enter a positive number in Mbit/s, or leave the limit blank.".to_string()
            })?;
        Ok((value > 0.0).then_some(value))
    }
    let down = rate(down)?;
    let up = rate(up)?;
    if down.is_none() && up.is_none() {
        Ok(Value::Null)
    } else {
        Ok(json!({"down": down, "up": up}))
    }
}

pub fn rate_text(value: Option<f64>) -> String {
    match value {
        None => "—".into(),
        Some(n) if n >= 1_000_000.0 => format!("{:.1} MB/s", n / 1_000_000.0),
        Some(n) if n >= 1_000.0 => format!("{:.0} kB/s", n / 1_000.0),
        Some(n) => format!("{n:.0} B/s"),
    }
}
