use intermux_common::{
    interface::Interface,
    protocol::{Event, Request, Response},
    state::{AdoptionView, BindingView},
    traffic::TrafficSample,
};
use serde::{de::DeserializeOwned, Serialize};
use serde_json::Value;

fn round_trip<T: DeserializeOwned + Serialize>(text: &str) {
    let expected: Value = serde_json::from_str(text).unwrap();
    let decoded: T = serde_json::from_str(text).unwrap();
    assert_eq!(serde_json::to_value(decoded).unwrap(), expected);
}

#[test]
fn contract_examples_round_trip() {
    round_trip::<Request>(r#"{"v":1,"id":7,"method":"bindings.list","params":{}}"#);
    round_trip::<Request>(
        r#"{"v":1,"id":1,"method":"hello","params":{"client":"cli","protocols":[1]}}"#,
    );
    round_trip::<Response>(r#"{"v":1,"id":7,"result":{"bindings":[],"adoptions":[]}}"#);
    round_trip::<Response>(
        r#"{"v":1,"id":7,"error":{"code":"unknown_method","message":"no method named foo"}}"#,
    );
    round_trip::<Response>(
        r#"{"v":1,"id":1,"result":{"daemon_version":"3.0.0-beta.1","protocol":1,"capabilities":["interfaces","bindings","traffic","gui"]}}"#,
    );
    round_trip::<Event>(
        r#"{"v":1,"event":"interface.status","data":{"interface":"wlan0","state":"connected"}}"#,
    );
    round_trip::<Interface>(
        r#"{"name":"wlan0","kind":"wifi","up":true,"bindable":true,"ipv4":["192.168.1.20/24"],"gateway":"192.168.1.1","state":"connected","routed":2,"link_speed":108000000.0,"dns":[],"limit":{"down":20.0,"up":null}}"#,
    );
    round_trip::<BindingView>(
        r#"{"iface":"wlan0","app":"/usr/bin/firefox","app_name":"firefox","namespace":"ns_1a2b3c4d","pid":4242,"uid":1000,"running":true,"pids":[4242,4250],"log_path":"/home/u/.local/state/intermux/logs/firefox-wlan0.log"}"#,
    );
    round_trip::<AdoptionView>(
        r#"{"id":"a87f9e9dfa6450ab","iface":"wlan0","app":"/usr/bin/sleep","app_name":"sleep","uid":1000,"running":true,"pids":[192]}"#,
    );
    round_trip::<TrafficSample>(
        r#"{"t":1763452800.5,"interfaces":{"wlan0":{"down":12345.0,"up":678.0}},"namespaces":{"wlan0":{"down":1000.0,"up":50.0}}}"#,
    );
    for code in [
        "bad_request",
        "unknown_method",
        "unsupported_version",
        "internal",
    ] {
        round_trip::<Response>(&format!(
            r#"{{"v":1,"id":3,"error":{{"code":"{code}","message":"error"}}}}"#
        ));
    }
    for event in [
        "interface.added",
        "interface.removed",
        "route.lost",
        "route.restored",
        "binding.started",
        "binding.stopped",
        "adoption.started",
        "adoption.stopped",
        "app.exited",
        "gui.raise",
        "daemon.shutdown",
    ] {
        round_trip::<Event>(&format!(r#"{{"v":1,"event":"{event}","data":{{}}}}"#));
    }
}
