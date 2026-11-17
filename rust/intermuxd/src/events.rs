use crate::view::View;
use intermux_common::protocol::Event;
use serde_json::json;
use std::collections::BTreeSet;

pub fn changes(
    old: &View,
    new: &View,
    lost_routes: &mut BTreeSet<String>,
) -> Vec<(&'static str, Event)> {
    let mut events = vec![];
    for i in &new.interfaces {
        match old.interfaces.iter().find(|o| o.name == i.name) {
            None => events.push(("interfaces", Event::new("interface.added", json!(i)))),
            Some(o) => {
                if i.status_changed(o) {
                    events.push(("interfaces", Event::new("interface.status", json!(i))));
                }
                if o.state == "connected" && i.state != "connected" && i.routed > 0 {
                    lost_routes.insert(i.name.clone());
                    events.push((
                        "interfaces",
                        Event::new("route.lost", json!({"interface": i.name})),
                    ));
                }
            }
        }
        if i.state == "connected" && i.routed > 0 && lost_routes.remove(&i.name) {
            events.push((
                "interfaces",
                Event::new("route.restored", json!({"interface": i.name})),
            ));
        }
        if i.routed == 0 {
            lost_routes.remove(&i.name);
        }
    }
    for i in &old.interfaces {
        if !new.interfaces.iter().any(|n| n.name == i.name) {
            lost_routes.remove(&i.name);
            events.push((
                "interfaces",
                Event::new("interface.removed", json!({"name": i.name})),
            ));
        }
    }
    for b in &new.bindings {
        match old.bindings.iter().find(|o| o.iface == b.iface && o.app == b.app && o.namespace == b.namespace) {
            None => events.push(("bindings", Event::new("binding.started", json!(b)))),
            Some(o) if o.running && !b.running => events.push(("bindings", Event::new("app.exited", json!({
                "iface": b.iface, "app_name": b.app_name, "uid": b.uid.or(o.uid), "expected": false
            })))),
            _ => (),
        }
    }
    for b in &old.bindings {
        if !new
            .bindings
            .iter()
            .any(|n| n.iface == b.iface && n.app == b.app && n.namespace == b.namespace)
        {
            events.push(("bindings", Event::new("binding.stopped", json!(b))));
        }
    }
    for a in &new.adoptions {
        if !old.adoptions.iter().any(|o| o.id == a.id) {
            events.push(("bindings", Event::new("adoption.started", json!(a))));
        }
    }
    for a in &old.adoptions {
        if !new.adoptions.iter().any(|n| n.id == a.id) {
            events.push(("bindings", Event::new("adoption.stopped", json!(a))));
        }
    }
    events
}
