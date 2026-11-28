use super::*;
use crate::model::{Binding, Interface, Rate, Selection, rate_text};
use rune::{LineCap, Shape, canvas};
use std::collections::VecDeque;

pub(super) fn kind(iface: &Interface) -> &'static str {
    match iface.kind.as_str() {
        "ethernet" => "Ethernet",
        "wifi" => "Wi-Fi",
        "usb" => "USB tether",
        "vpn" => "VPN tunnel",
        "mobile" => "Mobile",
        "bluetooth" => "Bluetooth",
        "virtual" => "Virtual",
        _ => "Interface",
    }
}

pub(super) fn route_color(iface: &Interface) -> &'static str {
    match iface.kind.as_str() {
        "wifi" => VIOLET,
        "usb" | "mobile" => GREEN,
        "vpn" => "#e7a8d8",
        _ => CYAN,
    }
}

fn interface_state(iface: &Interface) -> (&'static str, &'static str) {
    match iface.state.as_str() {
        "missing" => ("Missing", AMBER),
        "down" => ("Disconnected", AMBER),
        _ if !iface.bindable => ("No usable route", AMBER),
        _ => ("Connected", GREEN),
    }
}

fn mark(iface: &Interface, large: bool) -> El {
    let color = route_color(iface);
    let side = if large { 42.0 } else { 33.0 };
    let glyph = match iface.kind.as_str() {
        "wifi" => "◉",
        "usb" | "mobile" => "↥",
        "vpn" => "◇",
        _ => "↔",
    };
    el().size(side, side)
        .radius(6.0)
        .center()
        .fill(ink(color).alpha(0.11))
        .border(1.0, ink(color).alpha(0.17))
        .child(mono(glyph, if large { 20.0 } else { 17.0 }, color))
}

pub(super) fn app_icon(state: &State, path: &str, color: &str, size: f32) -> El {
    let square = el()
        .size(size, size)
        .radius(5.0)
        .center()
        .fill(ink(color).alpha(0.11));
    if let Some(icon) = state.icons.get(path).filter(|image| image.is_ready()) {
        return square.child(el().size(size * 0.8, size * 0.8).pixels(icon.pixels()));
    }
    let ink = ink(color).alpha(0.74);
    square.child(
        col()
            .size(size * 0.5, size * 0.41)
            .radius(2.0)
            .border(1.25, ink)
            .child(
                el().w_full()
                    .h(size * 0.12)
                    .border_sides([0.0, 0.0, 1.0, 0.0], ink),
            ),
    )
}

fn rates_line(rates: Option<&Rate>) -> El {
    row()
        .gap(12.0)
        .center_y()
        .child(mono(
            format!("↓ {}", rate_text(rates.and_then(|v| v.down))),
            11.5,
            TEXT,
        ))
        .child(mono(
            format!("↑ {}", rate_text(rates.and_then(|v| v.up))),
            11.5,
            MUTED,
        ))
}

fn group(state: &State, iface: &Interface) -> El {
    let name = iface.name.clone();
    let selected = state.selection.as_ref() == Some(&Selection::Interface(name.clone()));
    let color = route_color(iface);
    let (status, status_color) = interface_state(iface);
    let apps: Vec<_> = state
        .bindings()
        .iter()
        .filter(|app| app.iface == name)
        .collect();
    let mut card = panel()
        .w_full()
        .key(format!("group:{name}"))
        .clip_rounded(7.0)
        .child(
            pressable(
                row()
                    .key(format!("iface:{name}"))
                    .w_full()
                    .h(59.0)
                    .gap(10.0)
                    .px(12.0)
                    .center_y()
                    .border_sides([0.0, 0.0, 0.0, 2.0], ink(color))
                    .fill(ink(if selected { ACTIVE } else { PANEL }))
                    .hover(ink(HOVER))
                    .child(mark(iface, false))
                    .child(
                        col()
                            .gap(2.0)
                            .min_w(0.0)
                            .grow(1.0)
                            .child(copy(kind(iface), 16.0, 700.0, TEXT))
                            .child(mono(name.clone(), 11.0, MUTED).truncate()),
                    )
                    .child(if state.narrow {
                        el()
                    } else {
                        rates_line(iface.rates.as_ref())
                    })
                    .child(badge(status, status_color)),
                &format!("{} {}. {status}", kind(iface), name),
                true,
            )
            .on_press(move |s: &mut State| s.choose_interface(name.clone())),
        );
    if apps.is_empty() {
        card = card
            .child(rule())
            .child(row().h(41.0).px(14.0).center_y().child(copy(
                "No apps routed through this interface",
                13.0,
                450.0,
                FAINT,
            )));
    } else {
        for app in apps {
            card = card.child(rule()).child(app_row(state, app, color));
        }
    }
    card
}

fn app_row(state: &State, app: &Binding, color: &str) -> El {
    let key = app.key();
    let status = match (app.running, app.kind.as_str()) {
        (true, "shifted") => "Shifted",
        (true, _) => "Routed",
        (false, _) => "Stopped",
    };
    let selected = state.selection.as_ref() == Some(&Selection::App(key.clone()));
    pressable(
        row()
            .key(key.clone())
            .w_full()
            .h(46.0)
            .gap(9.0)
            .center_y()
            .px(14.0)
            .fill(ink(if selected { ACTIVE } else { PANEL }))
            .hover(ink(HOVER))
            .child(app_icon(state, &app.app, color, 26.0))
            .child(
                copy(app.name.clone(), 14.0, 630.0, TEXT)
                    .truncate()
                    .min_w(0.0)
                    .grow(1.0),
            )
            .child(copy(
                status,
                12.0,
                600.0,
                if app.running { color } else { FAINT },
            ))
            .child(copy("›", 19.0, 450.0, FAINT)),
        &format!("{}, {status}", app.name),
        true,
    )
    .on_press(move |s: &mut State| s.selection = Some(Selection::App(key.clone())))
}

fn graph(values: Option<&VecDeque<f64>>, color: &str) -> El {
    const WIDTH: f32 = 242.0;
    const HEIGHT: f32 = 88.0;
    // One canvas holds the gridlines, area and line, so a traffic tick rebuilds three
    // elements instead of one per sample.
    let mut shape = Shape::new();
    for i in 1..4 {
        let y = HEIGHT / 4.0 * i as f32 + 0.5;
        shape = shape.line([1.0, y], [WIDTH - 1.0, y], 1.0, LineCap::Butt, ink(LINE));
    }
    let chart = el()
        .size(WIDTH, HEIGHT)
        .radius(5.0)
        .clip_rounded(5.0)
        .fill(ink(BG))
        .border(1.0, ink(LINE));
    let Some(values) = values.filter(|series| series.len() > 1) else {
        return chart
            .child(
                canvas(shape)
                    .size(WIDTH, HEIGHT)
                    .absolute(0.0, 0.0)
                    .a11y_hidden(),
            )
            .child(el().w_full().h_full().center().child(copy(
                "Waiting for traffic samples",
                11.0,
                500.0,
                FAINT,
            )));
    };
    let peak = values.iter().copied().fold(32_768_f64, f64::max);
    let span = (WIDTH - 14.0) / 41.0;
    let line: Vec<[f32; 2]> = values
        .iter()
        .enumerate()
        .map(|(i, value)| {
            let remaining = values.len() - 1 - i;
            [
                WIDTH - 7.0 - span * remaining as f32,
                HEIGHT - 9.0 - (value / peak) as f32 * (HEIGHT - 19.0),
            ]
        })
        .collect();
    let [first_x, _] = line[0];
    let [x, y] = line[line.len() - 1];
    let mut area = Vec::with_capacity(line.len() + 2);
    area.push([first_x, HEIGHT - 1.0]);
    area.extend_from_slice(&line);
    area.push([x, HEIGHT - 1.0]);
    shape = shape
        .fill(area, ink(color).alpha(0.10))
        .stroke(line, 1.7, ink(color));
    chart
        .child(
            canvas(shape)
                .size(WIDTH, HEIGHT)
                .absolute(0.0, 0.0)
                .a11y_hidden(),
        )
        .child(
            el().size(5.0, 5.0)
                .radius(3.0)
                .absolute(x - 2.0, y - 2.0)
                .fill(ink(color)),
        )
        .label(format!(
            "Download traffic. Peak scale {}.",
            rate_text(Some(peak))
        ))
}

fn data_row(label: &str, value: impl Into<String>) -> El {
    row()
        .gap(8.0)
        .center_y()
        .child(copy(label, 12.0, 550.0, FAINT))
        .child(spacer())
        .child(mono(value, 11.0, TEXT).truncate().min_w(0.0))
}

fn details(state: &State) -> Option<El> {
    match &state.selection {
        Some(Selection::App(key)) => state
            .bindings()
            .iter()
            .find(|app| app.key() == *key)
            .map(|app| app_details(state, app)),
        Some(Selection::Interface(name)) => state
            .interfaces()
            .iter()
            .find(|i| i.name == *name)
            .map(|iface| interface_details(state, iface)),
        None => None,
    }
}

fn interface_details(state: &State, iface: &Interface) -> El {
    let (status, status_color) = interface_state(iface);
    let color = route_color(iface);
    let busy = state.busy().is_some();
    let name = iface.name.clone();
    let rate = iface.rates.as_ref();
    let editable = iface.has_namespace && !busy;
    let checkable = !busy
        && state
            .bindings()
            .iter()
            .any(|app| app.iface == iface.name && app.running && app.kind == "bound");
    let limit = iface.limit.as_ref().map(|r| {
        let rate = |v: Option<f64>| v.map_or("—".into(), |v| format!("{v}"));
        format!("↓ {} / ↑ {} Mbit/s", rate(r.down), rate(r.up))
    });
    let dns = if iface.dns.is_empty() {
        "Automatic".into()
    } else {
        iface.dns.join(", ")
    };
    panel().padding(15.0).gap(13.0)
        .child(row().gap(9.0).center_y().child(mark(iface, true)).child(col().gap(2.0).grow(1.0).min_w(0.0)
            .child(copy(kind(iface), 20.0, 720.0, TEXT))
            .child(mono(iface.name.clone(), 11.0, MUTED).truncate()))
            .child(badge(status, status_color)))
        .child(rule())
        .child(col().gap(8.0)
            .child(row().justify_between().center_y()
                .child(copy("Interface traffic", 13.0, 680.0, TEXT))
                .child(mono("Download", 10.0, color)))
            .child(graph(state.histories.get(&iface.name), color))
            .child(rates_line(rate)))
        .child(if iface.namespace_rates.is_some() {
            col().gap(6.0).child(copy("Shared traffic from routed apps", 12.0, 610.0, MUTED))
                .child(rates_line(iface.namespace_rates.as_ref()))
        } else { el() })
        .child(rule())
        .child(col().gap(8.0)
            .child(data_row("IPv4 address", iface.ipv4.first().cloned().unwrap_or_else(|| "—".into())))
            .child(data_row("DNS", dns))
            .child(data_row("Speed limit", limit.unwrap_or_else(|| "Unlimited".into())))
            .child(data_row("Public IP", state.public_ips.get(&iface.name).cloned().unwrap_or_else(|| "Unchecked".into()))))
        .child(row().gap(7.0).flex_wrap()
            .child(button("interface-verify", "Check public IP", Button::Quiet, checkable, {
                let name = name.clone();
                move |s| s.request("verify", json!({"iface": name}))
            }))
            .child(button("interface-dns", "Edit DNS", Button::Quiet, editable, {
                let iface = iface.clone();
                move |s| s.edit_interface(iface.clone(), true)
            }))
            .child(button("interface-limit", "Speed limit", Button::Quiet, editable, {
                let iface = iface.clone();
                move |s| s.edit_interface(iface.clone(), false)
            }))
            .child(button("interface-remove", "Remove namespace", Button::Quiet, editable, move |s| {
                s.confirm("Remove this namespace?", format!("Remove the routing, DNS and limits for {name}. Apps using it may need to be stopped first."),
                    "Remove namespace", "remove_namespace", json!({"iface": name}));
            })))
}

fn app_action(key: &str, label: &str, op: &'static str, app: &Binding, enabled: bool) -> El {
    let args = json!({"iface": app.iface, "app": app.app});
    button(key, label, Button::Quiet, enabled, move |s| {
        s.request(op, args.clone())
    })
}

fn app_details(state: &State, app: &Binding) -> El {
    let busy = state.busy().is_some();
    let shifted = app.kind == "shifted";
    let iface = app.iface.clone();
    let app_path = app.app.clone();
    let name = app.name.clone();
    let title = if app.running {
        if shifted { "Shifted" } else { "Routed" }
    } else {
        "Stopped"
    };
    let mut info = panel()
        .padding(15.0)
        .gap(13.0)
        .child(
            row()
                .gap(10.0)
                .center_y()
                .child(app_icon(state, &app.app, FOCUS, 41.0))
                .child(
                    copy(app.name.clone(), 18.0, 700.0, TEXT)
                        .grow(1.0)
                        .min_w(0.0)
                        .truncate(),
                )
                .child(badge(title, if app.running { GREEN } else { FAINT })),
        )
        .child(rule())
        .child(
            col()
                .gap(10.0)
                .child(data_row("Interface", app.iface.clone()))
                .child(data_row(
                    "Processes",
                    app.pids
                        .iter()
                        .map(u32::to_string)
                        .collect::<Vec<_>>()
                        .join(", "),
                ))
                .child(copy(app.app.clone(), 12.0, 480.0, MUTED).wrap_anywhere()),
        )
        .child(button(
            "show-interface",
            "Show interface details",
            Button::Quiet,
            true,
            move |s| s.choose_interface(iface.clone()),
        ));
    if shifted {
        let id = app.id.clone();
        info = info.child(button(
            "release-app",
            "Release session",
            Button::Quiet,
            !busy,
            move |s| {
                s.confirm(
                    "Release this session?",
                    "The running app will return to its default network route.".into(),
                    "Release session",
                    "release_adoption",
                    json!({"id": id}),
                );
            },
        ));
    } else {
        let item = app.clone();
        info = info.child(row().flex_wrap().gap(7.0)
            .child(app_action("restart-app", "Restart", "restart_app", app, !busy))
            .child(app_action("stop-app", "Stop", "stop_app", app, !busy && app.running))
            .child(app_action("open-log", "Open log", "open_log", app, !busy && app.log_path.is_some()))
            .child(button("move-app", "Move…", Button::Quiet,
                !busy && state.interfaces().iter().any(|i| i.bindable && i.name != app.iface),
                move |s| s.dialog = Some(crate::model::Dialog::Move(item.clone()))))
            .child(button("force-quit", "Force quit…", Button::Danger, !busy && app.running, move |s| {
                s.confirm("Force quit this application?", format!("All your running copies of {name} will close, including those outside InterMux."),
                    "Force quit", "kill_app", json!({"app": app_path, "force": true}));
            })));
    }
    info
}

pub(super) fn routes(state: &State) -> El {
    if state.interfaces().is_empty() {
        let (title, detail) = if state.snapshot.is_some() {
            (
                "No network interfaces available",
                "Connect to a network, then refresh to see available routes.",
            )
        } else {
            (
                "Reading your network",
                "The available interfaces and routed applications will appear here.",
            )
        };
        return panel()
            .w_full()
            .padding(25.0)
            .gap(7.0)
            .child(copy(title, 18.0, 650.0, TEXT))
            .child(copy(detail, 14.0, 450.0, MUTED).wrap());
    }
    let routed = state.bindings().iter().filter(|app| app.running).count();
    let num_interfaces = state.interfaces().len();
    let mut list = col().gap(11.0).grow(1.0).min_w(0.0).child(
        row()
            .gap(8.0)
            .center_y()
            .child(copy("Interfaces", 17.0, 690.0, TEXT))
            .child(spacer())
            .child(mono(
                format!("{num_interfaces} interfaces  /  {routed} routed"),
                11.0,
                MUTED,
            )),
    );
    for iface in state.interfaces() {
        list = list.child(group(state, iface));
    }
    let inspector = details(state).or_else(|| {
        state
            .interfaces()
            .first()
            .map(|iface| interface_details(state, iface))
    });
    if let Some(inspector) = inspector {
        let inspector = col()
            .gap(11.0)
            .child(copy("Details", 17.0, 690.0, TEXT))
            .child(inspector);
        if state.narrow {
            col().w_full().gap(19.0).child(list).child(inspector)
        } else {
            row()
                .w_full()
                .gap(19.0)
                .items_start()
                .child(list)
                .child(inspector.w(304.0))
        }
    } else {
        list
    }
}
