use intermux_common::{
    interface::parse_ip_json,
    state::{load_state, State},
    traffic::{iface_sample, link_speed_bytes, read_counters, veth_sample, RateTracker},
};
use serde_json::{json, Value};
use std::{
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

fn fixtures() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures")
}
fn json_file(name: &str) -> Value {
    serde_json::from_slice(&std::fs::read(fixtures().join(name)).unwrap()).unwrap()
}

#[test]
fn interfaces_match_shared_expected_answers() {
    let links = parse_ip_json(
        &json_file("ip-link.json"),
        &json_file("ip-addr.json"),
        &json_file("ip-route.json"),
        &fixtures().join("sys-class-net"),
    );
    let actual: Vec<Value> = links
        .iter()
        .map(|link| {
            let mut v = serde_json::to_value(link).unwrap();
            v["ipv4"] = json!(link.ipv4_cidrs());
            v["bindable"] = json!(link.bindable());
            v
        })
        .collect();
    assert_eq!(json!(actual), json_file("expected-interfaces.json"));
    assert!(parse_ip_json(&json!(null), &json!({}), &json!(false), &fixtures()).is_empty());
}

#[test]
fn state_fixtures_and_legacy_defaults() {
    let current = load_state(&fixtures().join("state-current.json")).unwrap();
    assert_eq!(current.bindings.len(), 1);
    assert_eq!(current.adoptions.len(), 1);
    assert_eq!(current.bindings[0].uid, Some(1000));
    assert_eq!(
        current.adoptions[0].origins["192"],
        "/user.slice/test.scope"
    );
    assert_eq!(
        serde_json::to_value(&current).unwrap(),
        json_file("state-current.json")
    );
    assert_eq!(
        current,
        load_state(&fixtures().join("state-mixed.json")).unwrap()
    );
    let old = load_state(&fixtures().join("state-old.json")).unwrap();
    assert_eq!(old.bindings[0].uid, None);
    assert_eq!(old.bindings[0].pid, None);
    assert!(old.adoptions[0].origins.is_empty());
    assert!(old.dns.is_empty());
    assert!(old.limits.is_empty());
    for name in [
        "state-invalid.json",
        "state-partial.json",
        "state-missing.json",
    ] {
        assert!(load_state(&fixtures().join(name)).is_err(), "{name}");
    }
    assert_eq!(
        State::from_value(&json!({"future":true})).unwrap(),
        State::default()
    );
}

#[test]
fn counters_direction_speed_and_resets() {
    let base = fixtures().join("sys-class-net");
    let counters = read_counters(
        [
            "eth0",
            "wlan0",
            "veth0_1a2b3c4d",
            "missing",
            "../eth0",
            "",
            ".",
            "..",
        ],
        &base,
    );
    assert_eq!(counters.len(), 3);
    assert_eq!(iface_sample(counters["eth0"]), (10000, 2000));
    assert_eq!(veth_sample(counters["veth0_1a2b3c4d"]), (2000, 10000));
    assert_eq!(link_speed_bytes("eth0", &base), Some(125_000_000.0));
    assert_eq!(link_speed_bytes("wlan0", &base), None);
    let mut tracker = RateTracker::default();
    let now = Instant::now();
    assert_eq!(
        tracker.update([("a".into(), (100, 200))].into(), now)["a"].down,
        0.0
    );
    let rate = tracker.update(
        [("a".into(), (300, 400))].into(),
        now + Duration::from_secs(2),
    );
    assert_eq!(rate["a"].down, 100.0);
    assert_eq!(rate["a"].up, 100.0);
    let reset = tracker.update(
        [("a".into(), (10, 20))].into(),
        now + Duration::from_secs(3),
    );
    assert_eq!(reset["a"].down, 0.0);
    assert_eq!(reset["a"].up, 0.0);
    assert!(tracker
        .update(Default::default(), now + Duration::from_secs(4))
        .is_empty());
    assert_eq!(
        tracker.update(
            [("a".into(), (500, 600))].into(),
            now + Duration::from_secs(5)
        )["a"]
            .down,
        0.0
    );
}
