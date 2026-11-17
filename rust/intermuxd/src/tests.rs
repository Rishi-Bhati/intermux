use crate::{
    config::Config,
    events,
    view::{self, Traffic},
};
use intermux_common::{interface::parse_ip_json, state::load_state};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
    time::Duration,
};

static SEQUENCE: AtomicUsize = AtomicUsize::new(0);
struct Tree(PathBuf);
impl Tree {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "intermuxd-unit-{}-{}",
            std::process::id(),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir_all(path.join("proc")).unwrap();
        fs::create_dir(path.join("netns")).unwrap();
        fs::create_dir(path.join("cgroups")).unwrap();
        Self(path)
    }
    fn process(&self, pid: u32, uid: u32, state: &str, ns: &str) {
        let dir = self.0.join("proc").join(pid.to_string());
        fs::create_dir_all(dir.join("ns")).unwrap();
        fs::write(
            dir.join("status"),
            format!("State:\t{state}\nPPid:\t1\nUid:\t{uid} {uid} {uid} {uid}\n"),
        )
        .unwrap();
        fs::write(dir.join("comm"), "app\n").unwrap();
        symlink(self.0.join("netns").join(ns), dir.join("ns/net")).unwrap();
    }
    fn config(&self) -> Config {
        Config {
            socket: self.0.join("d.sock"),
            state: fixtures().join("state-current.json"),
            sys_class_net: fixtures().join("sys-class-net"),
            proc: self.0.join("proc"),
            runtime_directory: None,
        }
    }
}
impl Drop for Tree {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}
fn input(name: &str) -> Value {
    serde_json::from_slice(&fs::read(fixtures().join(name)).unwrap()).unwrap()
}

#[test]
fn liveness_uses_inodes_cgroup_and_live_uid_for_old_bindings() {
    let tree = Tree::new();
    fs::write(tree.0.join("netns/ns_1a2b3c4d"), "").unwrap();
    fs::write(tree.0.join("netns/other"), "").unwrap();
    for (pid, state, ns) in [
        (4242, "S", "ns_1a2b3c4d"),
        (4250, "R", "ns_1a2b3c4d"),
        (4251, "Z", "ns_1a2b3c4d"),
        (4252, "X", "ns_1a2b3c4d"),
        (4253, "S", "other"),
        (192, "S", "other"),
    ] {
        tree.process(pid, 1234, state, ns);
    }
    let cgroup = tree.0.join("cgroups/intermux/adopt-a87f9e9dfa6450ab");
    fs::create_dir_all(&cgroup).unwrap();
    fs::write(cgroup.join("cgroup.procs"), "192\n4251\n999\n").unwrap();
    let mut state = load_state(&fixtures().join("state-current.json")).unwrap();
    state.bindings[0].uid = None;
    let links = parse_ip_json(
        &input("ip-link.json"),
        &input("ip-addr.json"),
        &input("ip-route.json"),
        &tree.config().sys_class_net,
    );
    let live = view::build(
        &state,
        &links,
        &tree.config(),
        &tree.0.join("netns"),
        &tree.0.join("cgroups"),
    );
    assert_eq!(live.bindings[0].pids, vec![4242, 4250]);
    assert_eq!(live.bindings[0].uid, Some(1234));
    assert!(live.bindings[0].running);
    assert_eq!(live.adoptions[0].pids, vec![192]);
    assert!(live.adoptions[0].running);
    assert_eq!(
        live.interfaces
            .iter()
            .find(|i| i.name == "wlan0")
            .unwrap()
            .routed,
        2
    );
    fs::remove_dir_all(tree.0.join("proc/4242")).unwrap();
    fs::remove_dir_all(tree.0.join("proc/4250")).unwrap();
    let dead = view::build(
        &state,
        &links,
        &tree.config(),
        &tree.0.join("netns"),
        &tree.0.join("cgroups"),
    );
    assert!(!dead.bindings[0].running);
    assert!(dead.bindings[0].pids.is_empty());
    let events = events::changes(&live, &dead, &mut BTreeSet::new());
    assert_eq!(
        events
            .iter()
            .filter(|(_, e)| e.event == "app.exited")
            .count(),
        1
    );
    assert_eq!(
        events
            .iter()
            .find(|(_, e)| e.event == "app.exited")
            .unwrap()
            .1
            .data,
        json!({"iface":"wlan0","app_name":"firefox","uid":1234,"expected":false})
    );
    assert!(events::changes(&dead, &dead, &mut BTreeSet::new()).is_empty());
    state.bindings.clear();
    let removed = view::build(
        &state,
        &links,
        &tree.config(),
        &tree.0.join("netns"),
        &tree.0.join("cgroups"),
    );
    let events = events::changes(&live, &removed, &mut BTreeSet::new());
    assert!(events.iter().any(|(_, e)| e.event == "binding.stopped"));
    assert!(!events.iter().any(|(_, e)| e.event == "app.exited"));
}

#[test]
fn visibility_and_route_transitions_include_bound_virtual_and_missing_links() {
    let tree = Tree::new();
    let mut state = load_state(&fixtures().join("state-current.json")).unwrap();
    let links = parse_ip_json(
        &input("ip-link.json"),
        &input("ip-addr.json"),
        &input("ip-route.json"),
        &tree.config().sys_class_net,
    );
    for name in ["eth1", "veth0_abcd1234", "absent", "lo"] {
        let mut b = state.bindings[0].clone();
        b.iface = name.into();
        state.bindings.push(b);
    }
    let mut live = view::build(
        &state,
        &links,
        &tree.config(),
        &tree.0.join("netns"),
        &tree.0.join("cgroups"),
    );
    assert!(!live.interfaces.iter().any(|i| i.name == "lo"));
    assert_eq!(
        live.interfaces
            .iter()
            .find(|i| i.name == "eth1")
            .unwrap()
            .state,
        "down"
    );
    assert_eq!(
        live.interfaces
            .iter()
            .find(|i| i.name == "absent")
            .unwrap()
            .state,
        "missing"
    );
    assert!(live.interfaces.iter().any(|i| i.name == "veth0_abcd1234"));
    let wlan = live
        .interfaces
        .iter_mut()
        .find(|i| i.name == "wlan0")
        .unwrap();
    wlan.routed = 2;
    let mut changed = live.clone();
    let wlan = changed
        .interfaces
        .iter_mut()
        .find(|i| i.name == "wlan0")
        .unwrap();
    wlan.dns = vec!["8.8.8.8".into()];
    wlan.link_speed = Some(5000.0);
    wlan.limit = None;
    wlan.kind = "other".into();
    let mut lost = BTreeSet::new();
    assert!(events::changes(&live, &changed, &mut lost).is_empty());
    let wlan = changed
        .interfaces
        .iter_mut()
        .find(|i| i.name == "wlan0")
        .unwrap();
    wlan.state = "missing".into();
    wlan.up = false;
    wlan.bindable = false;
    let changes = events::changes(&live, &changed, &mut lost);
    assert_eq!(
        changes
            .iter()
            .filter(|(_, e)| e.event == "route.lost")
            .count(),
        1
    );
    assert!(changes.iter().any(|(_, e)| e.event == "interface.status"));
    assert!(events::changes(&changed, &changed, &mut lost).is_empty());
    assert_eq!(
        events::changes(&changed, &live, &mut lost)
            .iter()
            .filter(|(_, e)| e.event == "route.restored")
            .count(),
        1
    );
    assert!(lost.is_empty());
    let mut removed = live.clone();
    removed.interfaces.retain(|i| i.name != "eth0");
    assert_eq!(
        events::changes(&live, &removed, &mut lost)
            .iter()
            .find(|(_, e)| e.event == "interface.removed")
            .unwrap()
            .1
            .data,
        json!({"name":"eth0"})
    );
}

#[test]
fn namespace_rates_use_python_veth_names_and_swapped_counters() {
    assert_eq!(view::host_veth("wlan0"), "veth0_70a15a82");
    let tree = Tree::new();
    let mut config = tree.config();
    config.sys_class_net = tree.0.join("sys-class-net");
    let physical = config.sys_class_net.join("wlan0/statistics");
    let host = config
        .sys_class_net
        .join(view::host_veth("wlan0"))
        .join("statistics");
    for dir in [&physical, &host] {
        fs::create_dir_all(dir).unwrap();
        fs::write(dir.join("rx_bytes"), "1000").unwrap();
        fs::write(dir.join("tx_bytes"), "2000").unwrap();
    }
    let state = load_state(&fixtures().join("state-current.json")).unwrap();
    let links = parse_ip_json(
        &input("ip-link.json"),
        &input("ip-addr.json"),
        &input("ip-route.json"),
        &config.sys_class_net,
    );
    let view = view::build(
        &state,
        &links,
        &config,
        &tree.0.join("netns"),
        &tree.0.join("cgroups"),
    );
    let mut traffic = Traffic::default();
    let first = traffic.sample(&view, &state, &config.sys_class_net);
    assert_eq!(first.namespaces["wlan0"].down, 0.0);
    std::thread::sleep(Duration::from_millis(10));
    fs::write(host.join("rx_bytes"), "1100").unwrap();
    fs::write(host.join("tx_bytes"), "2500").unwrap();
    let second = traffic.sample(&view, &state, &config.sys_class_net);
    assert!(second.namespaces["wlan0"].down > 0.0);
    assert!((second.namespaces["wlan0"].down / second.namespaces["wlan0"].up - 5.0).abs() < 0.001);
    assert_eq!(second.interfaces["wlan0"].down, 0.0);
}
