use crate::model::{App, Binding, Interface, Page, Rate, Selection, Snapshot, State, Vpn};

type Scene = (&'static str, fn(&mut State));

pub const SCENES: &[Scene] = &[
    ("dashboard", dashboard),
    ("composer", composer),
    ("vpn", vpn),
    ("queue", queue),
    ("empty", empty),
    ("running-choice", running_choice),
];

const DEMO_APPS: &[(&str, &str, &str, &[u8])] = &[
    (
        "Firefox",
        "/usr/lib/firefox/firefox",
        "Web browser",
        include_bytes!("../../assets/demo-icons/firefox.png"),
    ),
    (
        "qBittorrent",
        "/usr/bin/qbittorrent",
        "Download and manage torrents",
        include_bytes!("../../assets/demo-icons/qbittorrent.png"),
    ),
    (
        "Zoom",
        "/opt/zoom/zoom",
        "Video meetings",
        include_bytes!("../../assets/demo-icons/zoom.png"),
    ),
    (
        "Visual Studio Code",
        "/usr/share/code/code",
        "Source code editor",
        include_bytes!("../../assets/demo-icons/code.png"),
    ),
    (
        "Spotify",
        "/usr/bin/spotify",
        "Music and podcasts",
        include_bytes!("../../assets/demo-icons/spotify.png"),
    ),
    (
        "VLC",
        "/usr/bin/vlc",
        "Media player",
        include_bytes!("../../assets/demo-icons/vlc.png"),
    ),
    (
        "Steam",
        "/usr/bin/steam",
        "Games and downloads",
        include_bytes!("../../assets/demo-icons/steam.png"),
    ),
];

pub(crate) fn load_demo_icons(state: &mut State, cache: &rune::ImageCache) {
    for &(_, path, _, bytes) in DEMO_APPS {
        let image = cache.load(bytes);
        let _ = image.wait();
        state.icons.insert(path.into(), image);
    }
}

fn rate(down: f64, up: f64) -> Option<Rate> {
    Some(Rate {
        down: Some(down),
        up: Some(up),
    })
}

fn dashboard(s: &mut State) {
    s.demo = true;
    s.apps_loaded = true;
    s.apps_only = true;
    s.apps = DEMO_APPS
        .iter()
        .map(|&(name, path, description, _)| App {
            name: name.into(),
            path: path.into(),
            description: description.into(),
            bindable: true,
            recent: matches!(name, "Firefox" | "qBittorrent"),
            ..App::default()
        })
        .collect();
    let make_iface = |name: &str, kind: &str, ipv4: &str, down, up| Interface {
        name: name.into(),
        kind: kind.into(),
        state: "up".into(),
        bindable: true,
        ipv4: vec![ipv4.into()],
        rates: rate(down, up),
        namespace_rates: rate(down * 0.92, up * 0.85),
        has_namespace: true,
        ..Interface::default()
    };
    let mut ethernet = make_iface(
        "enp3s0",
        "ethernet",
        "192.168.0.24/24",
        8_420_000.0,
        740_000.0,
    );
    ethernet.dns = vec!["1.1.1.1".into()];
    let wifi = make_iface("wlan0", "wifi", "192.168.1.42/24", 2_170_000.0, 298_000.0);
    let usb = make_iface("usb0", "usb", "172.20.10.4/28", 682_000.0, 384_000.0);
    let binding = |iface: &str, index: usize, pid| Binding {
        iface: iface.into(),
        app: s.apps[index].path.clone(),
        name: s.apps[index].name.clone(),
        kind: "bound".into(),
        running: true,
        pids: vec![pid],
        log_path: Some(format!("/home/demo/.local/state/intermux/logs/{iface}.log")),
        ..Binding::default()
    };
    s.snapshot = Some(Snapshot {
        interfaces: vec![ethernet, wifi, usb],
        bindings: vec![
            binding("enp3s0", 1, 48211),
            binding("wlan0", 0, 48377),
            binding("usb0", 2, 48502),
        ],
        daemon_connected: true,
        vpn_available: true,
        ..Snapshot::default()
    });
    s.selection = Some(Selection::Interface("enp3s0".into()));
    s.histories.insert(
        "enp3s0".into(),
        [
            3.0, 3.7, 3.3, 4.0, 4.6, 4.3, 3.9, 4.9, 4.0, 4.6, 5.9, 5.2, 6.2, 5.1, 5.3, 4.1, 5.2,
            6.3, 7.7, 7.5, 8.2, 6.7, 6.9, 6.6, 6.3, 7.9, 6.8, 6.3, 7.2, 6.5, 6.6, 6.0, 6.8, 8.7,
            8.0, 7.9, 6.6, 7.6, 7.5, 7.2, 8.9, 8.42,
        ]
        .into_iter()
        .map(|value| value * 1_000_000.0)
        .collect(),
    );
    s.public_ips.insert("enp3s0".into(), "198.51.100.24".into());
}

fn composer(s: &mut State) {
    dashboard(s);
    s.compose(Some("wlan0".into()));
    s.composer.app = s.apps.first().cloned();
}

fn vpn(s: &mut State) {
    dashboard(s);
    s.page = Page::Vpn;
    s.vpn_loaded = true;
    s.vpns = vec![
        Vpn {
            name: "Work WireGuard".into(),
            uuid: "11111111-1111-1111-1111-111111111111".into(),
            kind: "wireguard".into(),
            active: true,
            device: Some("wg0".into()),
        },
        Vpn {
            name: "Travel VPN".into(),
            uuid: "22222222-2222-2222-2222-222222222222".into(),
            kind: "vpn".into(),
            active: false,
            device: None,
        },
    ];
}

fn queue(s: &mut State) {
    dashboard(s);
    for (app, iface) in [(3, "wlan0"), (4, "usb0")] {
        s.compose(Some(iface.into()));
        s.composer.app = Some(s.apps[app].clone());
        s.submit(false);
    }
}

fn running_choice(s: &mut State) {
    queue(s);
    if let Some(job) = s.queue.first() {
        s.dialog = Some(crate::model::Dialog::Running {
            id: job.id,
            count: 2,
        });
    }
}

fn empty(s: &mut State) {
    s.demo = true;
    s.snapshot = Some(Snapshot::default());
}

#[cfg(test)]
mod tests {
    use super::*;
    use rune::{Env, Runtime};
    use serde_json::json;
    use std::sync::mpsc;

    #[test]
    fn route_button_and_queue_actions_use_the_real_view() {
        let mut state = State::default();
        dashboard(&mut state);
        let mut runtime = Runtime::new(
            state,
            super::super::view,
            (1240.0, 775.0),
            Env {
                system_dark: true,
                scale: 1.0,
            },
        );
        runtime.frame(0.0);
        click(&mut runtime, "route-app");
        assert!(matches!(runtime.state.page, Page::Compose));
        click(&mut runtime, "choose-app-0");
        click(&mut runtime, "compose-interface-wlan0");
        click(&mut runtime, "queue-app");
        assert!(matches!(runtime.state.page, Page::Queue));
        assert_eq!(runtime.state.queue[0].iface, "wlan0");
        click(&mut runtime, "start-queue");
        assert_eq!(runtime.state.queue[0].outcome.as_deref(), Some("Started"));
    }

    #[test]
    fn vpn_navigation_and_settings_are_accessible_controls() {
        let mut state = State::default();
        dashboard(&mut state);
        let mut runtime = Runtime::new(
            state,
            super::super::view,
            (1240.0, 775.0),
            Env {
                system_dark: true,
                scale: 1.0,
            },
        );
        runtime.frame(0.0);
        click(&mut runtime, "nav-vpn");
        assert!(matches!(runtime.state.page, Page::Vpn));
        click(&mut runtime, "nav-settings");
        click(&mut runtime, "reset-routes");
        assert!(matches!(
            runtime.state.dialog,
            Some(crate::model::Dialog::Confirm { op: "reset", .. })
        ));
        runtime.key(rune::KeyEvent {
            key: rune::Key::Escape,
            mods: rune::Mods::default(),
        });
        runtime.frame(1.0 / 60.0);
        assert!(runtime.state.dialog.is_none());
    }

    #[test]
    fn queue_keeps_order_and_failed_items_after_an_already_running_choice() {
        let mut state = State::default();
        queue(&mut state);
        state.demo = false;
        let (send, requests) = mpsc::channel();
        state.attach(send);
        let ids: Vec<_> = state.queue.iter().map(|job| job.id).collect();
        let app = state.queue[0].app.path.clone();
        state.start(ids.clone());
        let crate::bridge::Input::Request(check) = requests.try_recv().unwrap() else {
            panic!("expected a request");
        };
        assert_eq!(check.op, "running_apps");
        state.receive(check, Ok(json!({(app.clone()): 1})));
        assert!(
            matches!(state.dialog, Some(crate::model::Dialog::Running { id, count: 1 }) if id == ids[0])
        );
        state.running_choice(ids[0], Some("shift"));
        let crate::bridge::Input::Request(batch) = requests.try_recv().unwrap() else {
            panic!("expected a request");
        };
        assert_eq!(batch.args["ops"][0]["op"], "adopt");
        assert_eq!(batch.args["ops"][0]["app"], app);
        assert_eq!(batch.args["ops"][1]["op"], "bind");
        state.receive(
            batch,
            Ok(json!([
                {"ok": true, "data": {}},
                {"ok": false, "error": {"code": "failed", "message": "Interface disappeared"}}
            ])),
        );
        assert_eq!(state.queue[0].outcome.as_deref(), Some("Session shifted"));
        assert_eq!(
            state.queue[1].issue.as_deref(),
            Some("Interface disappeared")
        );
        assert_eq!(state.queue[1].outcome, None);
    }

    #[test]
    fn status_message_keeps_route_controls_in_place() {
        let mut state = State::default();
        dashboard(&mut state);
        let mut runtime = Runtime::new(
            state,
            super::super::view,
            (1240.0, 775.0),
            Env {
                system_dark: true,
                scale: 1.0,
            },
        );
        runtime.frame(0.0);
        let route = runtime.find("iface:wlan0").expect("Wi-Fi row");
        runtime.update(|state, _| {
            state.status = Some(("The route was updated.".into(), false));
        });
        runtime.frame(1.0 / 60.0);
        assert!(runtime.find("dismiss-status").is_some());
        assert_eq!(runtime.find("iface:wlan0"), Some(route));
    }

    #[test]
    fn refresh_runs_a_force_scan_after_an_inflight_snapshot_finishes() {
        use crate::bridge::Input;

        let mut state = State::default();
        let (send, requests) = mpsc::channel();
        state.attach(send);
        state.request("snapshot", json!({}));
        let Input::Request(inflight) = requests.try_recv().unwrap() else {
            panic!("expected periodic snapshot");
        };
        for _ in 0..2 {
            state.request("snapshot", json!({"force": true}));
        }
        assert!(requests.try_recv().is_err());
        state.receive(inflight, Ok(json!({})));
        let Input::Request(forced) = requests.try_recv().unwrap() else {
            panic!("expected forced snapshot");
        };
        assert_eq!(forced.op, "snapshot");
        assert_eq!(forced.args, json!({"force": true}));
        state.receive(forced, Ok(json!({})));
        assert!(requests.try_recv().is_err());
    }

    fn click(runtime: &mut Runtime<State>, key: &str) {
        let (x, y) = runtime
            .find(key)
            .unwrap_or_else(|| panic!("no element {key}"));
        assert!(
            runtime.element_at(x, y).is_some(),
            "{key} is offscreen at {x},{y}"
        );
        runtime.pointer_move(x, y);
        runtime.pointer_down(x, y);
        runtime.frame(1.0 / 60.0);
        runtime.pointer_up(x, y);
        runtime.frame(1.0 / 60.0);
    }
}
