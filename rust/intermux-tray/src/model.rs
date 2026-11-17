//! Pure model: daemon data in, a plain description of the icon, tooltip and menu out.
use crate::format;
use intermux_common::{
    interface::Interface,
    state::{AdoptionView, BindingView, Limit},
    traffic::TrafficSample,
};
use serde_json::Value;
use std::{
    collections::{BTreeSet, HashMap},
    time::{Duration, Instant},
};

pub const VERSION: &str = env!("CARGO_PKG_VERSION");
pub const LIMIT_PRESETS: [f64; 6] = [1.0, 5.0, 10.0, 25.0, 50.0, 100.0];

#[derive(Debug, Clone, Default)]
pub struct Snapshot {
    pub connected: bool,
    pub interfaces: Vec<Interface>,
    pub bindings: Vec<BindingView>,
    pub adoptions: Vec<AdoptionView>,
    pub traffic: TrafficSample,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    Stop {
        iface: String,
        app: String,
        name: String,
    },
    Restart {
        iface: String,
        app: String,
        name: String,
    },
    Release {
        iface: String,
        id: String,
        name: String,
    },
    SetLimit {
        iface: String,
        down: Option<f64>,
        up: Option<f64>,
    },
    OpenGui,
    ToggleAutostart,
    Quit,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RadioOption {
    pub label: String,
    pub enabled: bool,
    pub action: Option<Action>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Node {
    /// Disabled, informational.
    Label(String),
    Separator,
    Item {
        label: String,
        enabled: bool,
        action: Action,
    },
    Check {
        label: String,
        checked: bool,
        action: Action,
    },
    Sub {
        label: String,
        children: Vec<Node>,
    },
    Radio {
        selected: usize,
        options: Vec<RadioOption>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IconState {
    Idle,
    Active,
    Attention,
}

#[derive(Debug, Clone, PartialEq)]
pub struct View {
    pub icon: IconState,
    pub title: String,
    pub tooltip: String,
    pub menu: Vec<Node>,
}

impl Snapshot {
    fn my_bindings<'a>(
        &'a self,
        uid: i64,
        iface: &'a str,
    ) -> impl Iterator<Item = &'a BindingView> {
        self.bindings
            .iter()
            .filter(move |b| b.uid == Some(uid) && b.iface == iface)
    }
    fn my_adoptions<'a>(
        &'a self,
        uid: i64,
        iface: &'a str,
    ) -> impl Iterator<Item = &'a AdoptionView> {
        self.adoptions
            .iter()
            .filter(move |a| a.uid == uid && a.iface == iface)
    }
    /// This user's bindings and adoptions on `iface`, in state (running or not).
    pub fn my_app_count(&self, uid: i64, iface: &str) -> usize {
        self.my_bindings(uid, iface).count() + self.my_adoptions(uid, iface).count()
    }
    fn my_running_count(&self, uid: i64, iface: &str) -> usize {
        self.my_bindings(uid, iface).filter(|b| b.running).count()
            + self.my_adoptions(uid, iface).filter(|a| a.running).count()
    }
}

fn speeds(snap: &Snapshot, iface: &Interface, sep: &str) -> String {
    if let Some(word) = format::state_word(&iface.state) {
        return word.into();
    }
    let rate = snap
        .traffic
        .interfaces
        .get(&iface.name)
        .cloned()
        .unwrap_or_default();
    format!(
        "↓ {}{sep}↑ {}",
        format::rate(rate.down),
        format::rate(rate.up)
    )
}

/// Index of the checked radio option and, for a non-preset limit, the custom label.
pub fn limit_selection(limit: Option<&Limit>) -> (usize, Option<String>) {
    let (down, up) = limit.map(|l| (l.down, l.up)).unwrap_or((None, None));
    match (down, up) {
        (None, None) => (0, None),
        (Some(d), Some(u)) if d == u => match LIMIT_PRESETS.iter().position(|p| *p == d) {
            Some(i) => (i + 1, None),
            None => (LIMIT_PRESETS.len() + 1, Some(custom_label(down, up))),
        },
        _ => (LIMIT_PRESETS.len() + 1, Some(custom_label(down, up))),
    }
}
fn custom_label(down: Option<f64>, up: Option<f64>) -> String {
    let side = |v: Option<f64>| v.map(format::mbit).unwrap_or_else(|| "-".into());
    format!("Custom ({} / {} Mbit/s)", side(down), side(up))
}

fn limit_menu(iface: &Interface, busy: bool) -> Node {
    let (selected, custom) = limit_selection(iface.limit.as_ref());
    let set = |v: Option<f64>| Action::SetLimit {
        iface: iface.name.clone(),
        down: v,
        up: v,
    };
    let mut options = vec![RadioOption {
        label: "No limit".into(),
        enabled: !busy,
        action: Some(set(None)),
    }];
    options.extend(LIMIT_PRESETS.iter().map(|p| RadioOption {
        label: format!("{} Mbit/s", format::mbit(*p)),
        enabled: !busy,
        action: Some(set(Some(*p))),
    }));
    if let Some(label) = custom {
        options.push(RadioOption {
            label,
            enabled: false,
            action: None,
        });
    }
    Node::Sub {
        label: "Speed limit".into(),
        children: vec![Node::Radio { selected, options }],
    }
}

fn interface_menu(snap: &Snapshot, iface: &Interface, uid: i64, busy: bool) -> Node {
    let mut children = Vec::new();
    for b in snap.my_bindings(uid, &iface.name) {
        let label = match (b.running, b.pids.first().map(|p| i64::from(*p)).or(b.pid)) {
            (true, Some(pid)) => format!("{} (pid {pid})", b.app_name),
            (true, None) => b.app_name.clone(),
            (false, _) => format!("{} (not running)", b.app_name),
        };
        let action = |restart: bool| {
            let (iface, app, name) = (b.iface.clone(), b.app.clone(), b.app_name.clone());
            if restart {
                Action::Restart { iface, app, name }
            } else {
                Action::Stop { iface, app, name }
            }
        };
        children.push(Node::Sub {
            label,
            children: vec![
                Node::Item {
                    label: "Restart".into(),
                    enabled: !busy,
                    action: action(true),
                },
                Node::Item {
                    label: "Stop".into(),
                    enabled: !busy,
                    action: action(false),
                },
            ],
        });
    }
    for a in snap.my_adoptions(uid, &iface.name) {
        children.push(Node::Sub {
            label: format!("{} (shifted)", a.app_name),
            children: vec![Node::Item {
                label: "Release".into(),
                enabled: !busy,
                action: Action::Release {
                    iface: a.iface.clone(),
                    id: a.id.clone(),
                    name: a.app_name.clone(),
                },
            }],
        });
    }
    if children.is_empty() {
        children.push(Node::Label("No apps".into()));
    }
    children.push(Node::Separator);
    children.push(limit_menu(iface, busy));
    Node::Sub {
        label: format!(
            "{} — {} — {}",
            iface.name,
            format::kind_label(&iface.kind),
            speeds(snap, iface, " ")
        ),
        children,
    }
}

pub fn build(snap: &Snapshot, uid: i64, busy: &BTreeSet<String>, autostart: bool) -> View {
    let mut menu = vec![Node::Label(format!("InterMux {VERSION}"))];
    let mut icon = IconState::Idle;
    let mut lines = Vec::new();
    let mut attention = Vec::new();
    let mut running = 0;
    if snap.connected {
        for iface in &snap.interfaces {
            menu.push(interface_menu(snap, iface, uid, busy.contains(&iface.name)));
            let mut line = format!("{}  {}", iface.name, speeds(snap, iface, "  "));
            if iface.routed > 0 {
                line.push_str(&format!("  · {}", format::apps(iface.routed)));
            }
            lines.push(line);
            running += snap.my_running_count(uid, &iface.name);
            if iface.state != "connected" && snap.my_app_count(uid, &iface.name) > 0 {
                attention.push(iface.name.clone());
            }
        }
        if running > 0 {
            icon = IconState::Active;
        }
        if !attention.is_empty() {
            icon = IconState::Attention;
        }
    } else {
        menu.push(Node::Label("Daemon not running".into()));
        lines.push("Daemon not running".into());
    }
    menu.extend([
        Node::Separator,
        Node::Item {
            label: "Open InterMux".into(),
            enabled: true,
            action: Action::OpenGui,
        },
        Node::Check {
            label: "Start at sign-in".into(),
            checked: autostart,
            action: Action::ToggleAutostart,
        },
        Node::Item {
            label: "Quit".into(),
            enabled: true,
            action: Action::Quit,
        },
    ]);
    let title = match icon {
        IconState::Idle => "InterMux".into(),
        IconState::Active => format!("InterMux — {} routed", format::apps(running)),
        IconState::Attention => format!("InterMux — {} needs attention", attention.join(", ")),
    };
    View {
        icon,
        title,
        tooltip: lines.join("\n"),
        menu,
    }
}

/// The notification for a daemon event, keyed by interface; `None` if this user should not see one.
pub fn notification(
    event: &str,
    data: &Value,
    snap: &Snapshot,
    uid: i64,
) -> Option<(String, String)> {
    let text = |key: &str| data[key].as_str().map(str::to_string);
    match event {
        "route.lost" | "route.restored" => {
            let iface = text("interface")?;
            let count = snap.my_app_count(uid, &iface);
            if count == 0 {
                return None;
            }
            let message = if event == "route.lost" {
                let verb = if count == 1 { "lost its" } else { "lost their" };
                format!(
                    "{iface} is down — {} {verb} connection",
                    format::apps(count)
                )
            } else {
                format!("{iface} is back")
            };
            Some((iface, message))
        }
        "app.exited" if data["uid"].as_i64() == Some(uid) => {
            let iface = text("iface")?;
            let name = text("app_name")?;
            Some((iface.clone(), format!("{name} on {iface} has exited")))
        }
        _ => None,
    }
}

/// At most one notification per key per interval.
pub struct RateLimiter {
    interval: Duration,
    last: HashMap<String, Instant>,
}
impl RateLimiter {
    pub fn new(interval: Duration) -> Self {
        Self {
            interval,
            last: HashMap::new(),
        }
    }
    pub fn allow(&mut self, key: &str, now: Instant) -> bool {
        match self.last.get(key) {
            Some(t) if now.duration_since(*t) < self.interval => false,
            _ => {
                self.last.insert(key.into(), now);
                true
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use intermux_common::traffic::Rate;
    use serde_json::json;

    const ME: i64 = 1000;

    fn iface(name: &str, state: &str, routed: usize, limit: Option<Limit>) -> Interface {
        serde_json::from_value(json!({
            "name": name, "kind": if name.starts_with("wg") { "vpn" } else { "wifi" },
            "up": state != "down", "bindable": state == "connected", "ipv4": [], "gateway": null,
            "state": state, "routed": routed, "link_speed": null, "dns": [], "limit": limit,
        }))
        .unwrap()
    }
    fn binding(iface: &str, name: &str, uid: i64, pid: u32) -> BindingView {
        serde_json::from_value(json!({
            "iface": iface, "app": format!("/usr/bin/{name}"), "app_name": name, "namespace": "ns_1",
            "pid": pid, "uid": uid, "running": true, "pids": [pid], "log_path": "/x.log",
        }))
        .unwrap()
    }
    fn adoption(iface: &str, name: &str, uid: i64) -> AdoptionView {
        serde_json::from_value(json!({
            "id": "a87f9e9dfa6450ab", "iface": iface, "app": format!("/usr/bin/{name}"),
            "app_name": name, "uid": uid, "running": true, "pids": [192],
        }))
        .unwrap()
    }
    fn snap() -> Snapshot {
        let mut traffic = TrafficSample::default();
        traffic.interfaces.insert(
            "wlan0".into(),
            Rate {
                down: 4.1 * 1024.0 * 1024.0,
                up: 120.0 * 1024.0,
            },
        );
        Snapshot {
            connected: true,
            interfaces: vec![
                iface("wlan0", "connected", 3, None),
                iface("wg0", "connected", 0, None),
            ],
            bindings: vec![
                binding("wlan0", "firefox", ME, 4242),
                binding("wlan0", "chromium", 1001, 99),
            ],
            adoptions: vec![adoption("wlan0", "sleep", ME)],
            traffic,
        }
    }
    fn sub<'a>(nodes: &'a [Node], prefix: &str) -> &'a [Node] {
        nodes
            .iter()
            .find_map(|n| match n {
                Node::Sub { label, children } if label.starts_with(prefix) => {
                    Some(children.as_slice())
                }
                _ => None,
            })
            .unwrap_or_else(|| panic!("no submenu {prefix}"))
    }
    fn labels(nodes: &[Node]) -> Vec<String> {
        nodes
            .iter()
            .map(|n| match n {
                Node::Label(l)
                | Node::Item { label: l, .. }
                | Node::Check { label: l, .. }
                | Node::Sub { label: l, .. } => l.clone(),
                Node::Separator => "-".into(),
                Node::Radio { .. } => "radio".into(),
            })
            .collect()
    }

    #[test]
    fn menu_shows_only_own_apps() {
        let view = build(&snap(), ME, &BTreeSet::new(), false);
        assert_eq!(
            labels(&view.menu),
            [
                format!("InterMux {VERSION}"),
                "wlan0 — Wi-Fi — ↓ 4.1 MB/s ↑ 120 KB/s".into(),
                "wg0 — VPN — ↓ 0 B/s ↑ 0 B/s".into(),
                "-".into(),
                "Open InterMux".into(),
                "Start at sign-in".into(),
                "Quit".into(),
            ]
        );
        let wlan = sub(&view.menu, "wlan0");
        assert_eq!(
            labels(wlan),
            ["firefox (pid 4242)", "sleep (shifted)", "-", "Speed limit"]
        );
        assert_eq!(labels(sub(wlan, "firefox")), ["Restart", "Stop"]);
        assert_eq!(
            sub(wlan, "sleep")[0],
            Node::Item {
                label: "Release".into(),
                enabled: true,
                action: Action::Release {
                    iface: "wlan0".into(),
                    id: "a87f9e9dfa6450ab".into(),
                    name: "sleep".into()
                },
            }
        );
        assert_eq!(
            labels(sub(&view.menu, "wg0")),
            ["No apps", "-", "Speed limit"]
        );
        // The other user's chromium still counts in routed.
        assert_eq!(
            view.tooltip,
            "wlan0  ↓ 4.1 MB/s  ↑ 120 KB/s  · 3 apps\nwg0  ↓ 0 B/s  ↑ 0 B/s"
        );
        assert_eq!(view.icon, IconState::Active);
        // Another user sees only their own app.
        let other = build(&snap(), 1001, &BTreeSet::new(), false);
        assert_eq!(labels(sub(&other.menu, "wlan0"))[0], "chromium (pid 99)");
    }

    #[test]
    fn icon_states() {
        let mut s = snap();
        s.bindings.retain(|b| b.uid != Some(ME));
        s.adoptions.clear();
        assert_eq!(build(&s, ME, &BTreeSet::new(), false).icon, IconState::Idle);
        // Someone else's app on a lost link does not need this user's attention.
        s.interfaces[0].state = "down".into();
        assert_eq!(build(&s, ME, &BTreeSet::new(), false).icon, IconState::Idle);

        for state in ["down", "no_route", "missing"] {
            let mut s = snap();
            s.interfaces[0].state = state.into();
            let view = build(&s, ME, &BTreeSet::new(), false);
            assert_eq!(view.icon, IconState::Attention, "{state}");
            assert_eq!(view.title, "InterMux — wlan0 needs attention");
            assert!(view
                .tooltip
                .starts_with(&format!("wlan0  {}", format::state_word(state).unwrap())));
        }
        let mut s = snap();
        s.interfaces[0].state = "no_route".into();
        assert_eq!(
            labels(&build(&s, ME, &BTreeSet::new(), false).menu)[1],
            "wlan0 — Wi-Fi — No gateway"
        );
    }

    #[test]
    fn disconnected() {
        let view = build(&Snapshot::default(), ME, &BTreeSet::new(), true);
        assert_eq!(view.icon, IconState::Idle);
        assert_eq!(labels(&view.menu)[1], "Daemon not running");
        assert!(matches!(view.menu[4], Node::Check { checked: true, .. }));
    }

    fn radio(view: &View, iface: &str) -> (usize, Vec<RadioOption>) {
        match &sub(sub(&view.menu, iface), "Speed limit")[0] {
            Node::Radio { selected, options } => (*selected, options.clone()),
            other => panic!("{other:?}"),
        }
    }

    #[test]
    fn limit_radio() {
        let lim = |d, u| Some(Limit { down: d, up: u });
        assert_eq!(limit_selection(None), (0, None));
        assert_eq!(limit_selection(lim(None, None).as_ref()), (0, None));
        assert_eq!(
            limit_selection(lim(Some(1.0), Some(1.0)).as_ref()),
            (1, None)
        );
        assert_eq!(
            limit_selection(lim(Some(100.0), Some(100.0)).as_ref()),
            (6, None)
        );
        assert_eq!(
            limit_selection(lim(Some(20.0), None).as_ref()),
            (7, Some("Custom (20 / - Mbit/s)".into()))
        );
        assert_eq!(
            limit_selection(lim(Some(5.0), Some(2.5)).as_ref()),
            (7, Some("Custom (5 / 2.5 Mbit/s)".into()))
        );
        assert_eq!(
            limit_selection(lim(Some(7.0), Some(7.0)).as_ref()),
            (7, Some("Custom (7 / 7 Mbit/s)".into()))
        );

        let mut s = snap();
        s.interfaces[0].limit = lim(Some(10.0), Some(10.0));
        s.interfaces[1].limit = lim(Some(20.0), None);
        let view = build(&s, ME, &BTreeSet::new(), false);
        let (selected, options) = radio(&view, "wlan0");
        assert_eq!(selected, 3);
        let names: Vec<_> = options.iter().map(|o| o.label.as_str()).collect();
        assert_eq!(
            names,
            [
                "No limit",
                "1 Mbit/s",
                "5 Mbit/s",
                "10 Mbit/s",
                "25 Mbit/s",
                "50 Mbit/s",
                "100 Mbit/s"
            ]
        );
        assert_eq!(
            options[0].action,
            Some(Action::SetLimit {
                iface: "wlan0".into(),
                down: None,
                up: None
            })
        );
        assert_eq!(
            options[4].action,
            Some(Action::SetLimit {
                iface: "wlan0".into(),
                down: Some(25.0),
                up: Some(25.0)
            })
        );
        let (selected, options) = radio(&view, "wg0");
        assert_eq!(selected, 7);
        assert_eq!(
            options[7],
            RadioOption {
                label: "Custom (20 / - Mbit/s)".into(),
                enabled: false,
                action: None
            }
        );
    }

    #[test]
    fn busy_interface_disables_its_actions() {
        let busy = BTreeSet::from(["wlan0".to_string()]);
        let view = build(&snap(), ME, &busy, false);
        let wlan = sub(&view.menu, "wlan0");
        assert!(sub(wlan, "firefox")
            .iter()
            .all(|n| matches!(n, Node::Item { enabled: false, .. })));
        assert!(matches!(
            sub(wlan, "sleep")[0],
            Node::Item { enabled: false, .. }
        ));
        assert!(radio(&view, "wlan0").1.iter().all(|o| !o.enabled));
        assert!(radio(&view, "wg0").1.iter().all(|o| o.enabled));
    }

    #[test]
    fn notifications() {
        let s = snap();
        let n = |e: &str, d: Value| notification(e, &d, &s, ME);
        assert_eq!(
            n("route.lost", json!({"interface": "wlan0"})),
            Some((
                "wlan0".into(),
                "wlan0 is down — 2 apps lost their connection".into()
            ))
        );
        assert_eq!(
            n("route.restored", json!({"interface": "wlan0"})),
            Some(("wlan0".into(), "wlan0 is back".into()))
        );
        assert_eq!(n("route.lost", json!({"interface": "wg0"})), None);
        let exited = json!({"iface": "wlan0", "app_name": "firefox", "uid": ME, "expected": false});
        assert_eq!(
            n("app.exited", exited),
            Some(("wlan0".into(), "firefox on wlan0 has exited".into()))
        );
        assert_eq!(
            n(
                "app.exited",
                json!({"iface": "wlan0", "app_name": "chromium", "uid": 1001})
            ),
            None
        );
        let mut one = snap();
        one.adoptions.clear();
        assert_eq!(
            notification("route.lost", &json!({"interface": "wlan0"}), &one, ME)
                .unwrap()
                .1,
            "wlan0 is down — 1 app lost its connection"
        );
    }

    #[test]
    fn rate_limit() {
        let mut limiter = RateLimiter::new(Duration::from_secs(10));
        let t = Instant::now();
        assert!(limiter.allow("wg0", t));
        assert!(!limiter.allow("wg0", t + Duration::from_secs(9)));
        assert!(limiter.allow("wlan0", t + Duration::from_secs(9)));
        assert!(limiter.allow("wg0", t + Duration::from_secs(10)));
    }
}
