mod dashboard;
mod dialogs;
mod fixtures;
mod pages;

use crate::model::{Page, State};
use rune::{
    Color, Cursor, Cx, El, Key, KeyEvent, ScrollSpec, TextEdit, TextStyle, Tx, col, el, row, text,
    text_input,
};
use serde_json::json;

pub use fixtures::SCENES;
pub(crate) use fixtures::load_demo_icons;

const BG: &str = "#0c0d0e";
const RAIL: &str = "#101113";
const PANEL: &str = "#121416";
const HOVER: &str = "#1b1e22";
const ACTIVE: &str = "#1b2138";
const LINE: &str = "#282c32";
const BORDER: &str = "#383e48";
const TEXT: &str = "#e9eaeb";
const MUTED: &str = "#a8afb9";
const FAINT: &str = "#89939e";
const ACCENT: &str = "#3a5aff";
const FOCUS: &str = "#a9b7ff";
const GREEN: &str = "#3fd69a";
const RED: &str = "#ff9898";
const AMBER: &str = "#ffbc68";
const CYAN: &str = "#55d4f3";
const VIOLET: &str = "#b299ff";

fn ink(color: &str) -> Color {
    rune::color::hex(color).expect("valid palette color")
}

fn copy(label: impl Into<String>, size: f32, weight: f32, color: &str) -> El {
    text(label.into())
        .font(TextStyle::new(size, weight))
        .color(ink(color))
}

fn mono(label: impl Into<String>, size: f32, color: &str) -> El {
    text(label.into())
        .font(TextStyle::new(size, 500.0).mono())
        .color(ink(color))
}

fn rule() -> El {
    el().w_full().h(1.0).fill(ink(LINE))
}
fn spacer() -> El {
    el().grow(1.0).min_w(0.0)
}
fn panel() -> El {
    col().fill(ink(PANEL)).border(1.0, ink(LINE)).radius(8.0)
}
fn scroll(contents: El) -> El {
    contents
        .scroll(ScrollSpec {
            stick_bottom: false,
        })
        .min_h(0.0)
        .shrink()
}

#[derive(Clone, Copy)]
enum Button {
    Primary,
    Quiet,
    Danger,
}

fn pressable(surface: El, label: &str, enabled: bool) -> El {
    surface
        .role("button")
        .label(label)
        .disabled(!enabled)
        .focusable(enabled)
        .focus_ring(2.0, 2.0, ink(FOCUS))
        .cursor(if enabled {
            Cursor::Pointer
        } else {
            Cursor::Default
        })
}

fn button_tx(
    key: impl Into<String>,
    label: &str,
    appearance: Button,
    enabled: bool,
    action: impl Fn(&mut State, &mut Tx) + 'static,
) -> El {
    let (fill, edge, color, hover) = match (appearance, enabled) {
        (_, false) => (PANEL, LINE, FAINT, PANEL),
        (Button::Primary, true) => (ACCENT, "#6278ff", "#ffffff", "#4966ff"),
        (Button::Quiet, true) => (PANEL, BORDER, TEXT, HOVER),
        (Button::Danger, true) => ("#261719", "#6c393c", RED, "#402322"),
    };
    let button = pressable(
        row()
            .key(key.into())
            .h(36.0)
            .px(12.0)
            .center()
            .radius(5.0)
            .border(1.0, ink(edge))
            .fill(ink(fill))
            .hover(ink(hover))
            .child(copy(label, 13.0, 600.0, color)),
        label,
        enabled,
    );
    if enabled {
        button.on_press_tx(move |state, tx| {
            tx.instant();
            action(state, tx);
        })
    } else {
        button
    }
}

fn button(
    key: impl Into<String>,
    label: &str,
    appearance: Button,
    enabled: bool,
    action: impl Fn(&mut State) + 'static,
) -> El {
    button_tx(key, label, appearance, enabled, move |state, _| {
        action(state)
    })
}

fn field(
    key: &str,
    label: &str,
    placeholder: &str,
    edit: &TextEdit,
    access: fn(&mut State) -> &mut TextEdit,
) -> El {
    col()
        .gap(6.0)
        .min_w(0.0)
        .child(copy(label, 13.0, 600.0, TEXT))
        .child(
            text_input(edit, TextStyle::new(14.0, 400.0))
                .key(key.to_string())
                .label(label)
                .single_line()
                .h(38.0)
                .w_full()
                .padding(10.0)
                .radius(5.0)
                .border(1.0, ink(BORDER))
                .fill(ink(BG))
                .color(ink(TEXT))
                .placeholder(placeholder, ink(FAINT))
                .caret(ink(TEXT), ink(ACCENT))
                .focus_ring(2.0, 2.0, ink(FOCUS))
                .on_edit(access),
        )
}

fn badge(label: &str, color: &str) -> El {
    row()
        .h(22.0)
        .px(7.0)
        .gap(6.0)
        .center_y()
        .radius(4.0)
        .fill(ink(color).alpha(0.09))
        .child(el().size(5.0, 5.0).radius(3.0).fill(ink(color)))
        .child(copy(label, 11.0, 700.0, color))
}

fn heading(label: &str, detail: &str) -> El {
    col()
        .gap(5.0)
        .min_w(0.0)
        .child(copy(label, 35.0, 700.0, TEXT).role("heading"))
        .child(copy(detail, 14.0, 400.0, MUTED).wrap())
}

fn open_composer(state: &mut State, tx: &mut Tx) {
    state.compose(None);
    tx.focus("app-search", true);
}

fn page_title(page: Page) -> (&'static str, &'static str) {
    match page {
        Page::Routes => (
            "Network routes",
            "See which connection each application is using.",
        ),
        Page::Compose => (
            "Route an app",
            "Choose an application and the connection it should use.",
        ),
        Page::Vpn => (
            "VPN connections",
            "Connect a tunnel and choose which apps use it.",
        ),
        Page::Settings => (
            "Settings",
            "Manage authorization, recovery, and diagnostics.",
        ),
        Page::Queue => (
            "Launch queue",
            "Review routing changes and launch queued apps together.",
        ),
    }
}

fn nav_item(
    key: &str,
    label: &str,
    glyph: &str,
    page: Page,
    current: Page,
    count: Option<usize>,
) -> El {
    let selected = page == current;
    let mut item = row()
        .key(key.to_string())
        .h(40.0)
        .w_full()
        .px(10.0)
        .gap(9.0)
        .center_y()
        .radius(5.0)
        .fill(if selected {
            ink(ACTIVE)
        } else {
            Color::TRANSPARENT
        })
        .hover(ink(HOVER))
        .child(mono(glyph, 15.0, if selected { FOCUS } else { FAINT }).w(18.0))
        .child(copy(
            label,
            13.0,
            650.0,
            if selected { TEXT } else { MUTED },
        ));
    if let Some(count) = count {
        item = item
            .child(spacer())
            .child(mono(count.to_string(), 11.0, FOCUS));
    }
    pressable(item, label, true).on_press_tx(move |state: &mut State, tx| {
        tx.instant();
        state.change_page(page);
    })
}

fn brand(state: &State) -> El {
    let mut mark = el().size(25.0, 25.0);
    if let Some(logo) = &state.logo {
        mark = mark.pixels(logo.pixels());
    }
    row()
        .gap(9.0)
        .center_y()
        .child(mark)
        .child(copy("InterMux", 22.0, 740.0, TEXT))
}

fn sidebar(state: &State) -> El {
    let pending = state
        .queue
        .iter()
        .filter(|job| job.outcome.is_none())
        .count();
    let mut rail = col()
        .w(184.0)
        .h_full()
        .padding(16.0)
        .gap(22.0)
        .fill(ink(RAIL))
        .border_sides([0.0, 1.0, 0.0, 0.0], ink(LINE))
        .child(brand(state))
        .child({
            let mut nav = col()
                .gap(5.0)
                .child(nav_item(
                    "nav-routes",
                    "Routes",
                    "⇄",
                    Page::Routes,
                    state.page,
                    None,
                ))
                .child(nav_item("nav-vpn", "VPN", "◇", Page::Vpn, state.page, None))
                .child(nav_item(
                    "nav-settings",
                    "Settings",
                    "⚙",
                    Page::Settings,
                    state.page,
                    None,
                ));
            if pending > 0 || !state.queue.is_empty() || state.page == Page::Queue {
                nav = nav.child(nav_item(
                    "nav-queue",
                    "Launch queue",
                    "≡",
                    Page::Queue,
                    state.page,
                    Some(pending),
                ));
            }
            nav
        })
        .child(spacer())
        .child(rule());
    let (label, color) = match state.snapshot.as_ref() {
        _ if state.demo => ("Sample data", FAINT),
        _ if state.snapshot_error.is_some() => ("Reconnecting", AMBER),
        Some(s) if s.daemon_connected => ("Live updates", GREEN),
        Some(_) => ("Local updates", FAINT),
        None => ("Connecting", FAINT),
    };
    rail = rail.child(
        row()
            .gap(7.0)
            .center_y()
            .child(el().size(6.0, 6.0).radius(3.0).fill(ink(color)))
            .child(copy(label, 12.0, 600.0, color)),
    );
    rail
}

fn status(state: &State) -> Option<El> {
    let error = state
        .snapshot_error
        .as_ref()
        .map(|message| (message.as_str(), true));
    let action = state
        .status
        .as_ref()
        .map(|(message, problem)| (message.as_str(), *problem));
    let busy = state.busy().map(|op| {
        (
            match op {
                "batch" => "Starting apps. Respond to the authorization prompt if one appears.",
                "running_apps" => "Checking for running copies…",
                "resolve_app" => "Checking the application…",
                "save_report" => "Collecting diagnostics…",
                "vpn_connect" => "Connecting the VPN…",
                "vpn_disconnect" => "Disconnecting the VPN…",
                _ => "Working…",
            },
            false,
        )
    });
    let (message, problem) = error.or(busy).or(action)?;
    let color = if problem { AMBER } else { FOCUS };
    let mut bar = row()
        .center_y()
        .gap(12.0)
        .padding(9.0)
        .radius(5.0)
        .border(1.0, ink(color).alpha(0.18))
        .fill(ink(color).alpha(0.065))
        .role("status")
        .child(
            copy(message, 13.0, 500.0, color)
                .grow(1.0)
                .min_w(0.0)
                .wrap(),
        );
    if error.is_none() && busy.is_none() {
        bar = bar.child(button(
            "dismiss-status",
            "Dismiss",
            Button::Quiet,
            true,
            |s| s.status = None,
        ));
    }
    Some(bar)
}

pub fn view(state: &State, ctx: &mut Cx<State>) -> El {
    ctx.foreground(ink(TEXT));
    ctx.focus_ring(2.0, 2.0, ink(FOCUS));
    let (title, detail) = page_title(state.page);
    let title_block = heading(title, detail);
    let header = if state.page != Page::Routes {
        title_block
    } else {
        let actions = row()
            .gap(8.0)
            .child(button("refresh", "Refresh", Button::Quiet, true, |s| {
                s.request("snapshot", json!({"force": true}));
                s.request("apps", json!({}));
            }))
            .child(button_tx(
                "route-app",
                "+  Route an app",
                Button::Primary,
                true,
                open_composer,
            ));
        if state.narrow {
            col().gap(13.0).child(title_block).child(actions)
        } else {
            row()
                .center_y()
                .gap(12.0)
                .child(title_block.grow(1.0))
                .child(actions)
        }
    };
    let content = match state.page {
        Page::Routes => dashboard::routes(state),
        Page::Compose => pages::composer(state),
        Page::Queue => pages::queue(state),
        Page::Vpn => pages::vpn(state),
        Page::Settings => pages::settings(state),
    };
    let mut body = col()
        .w_full()
        .padding(if state.narrow { 19.0 } else { 28.0 })
        .gap(21.0)
        .child(header);
    if let Some(snapshot) = &state.snapshot
        && !snapshot.dependencies.is_empty()
        && state.page == Page::Routes
    {
        body = body.child(
            copy(
                format!(
                    "Required tools missing: {}",
                    snapshot.dependencies.join(", ")
                ),
                13.0,
                550.0,
                AMBER,
            )
            .wrap(),
        );
    }
    let mut main = col().w(0.0).grow(1.0).min_w(0.0).h_full().child(
        scroll(body.child(content))
            .w_full()
            .h(0.0)
            .grow(1.0)
            .transition(rune::Transition::none())
            .key(format!("page-{title}")),
    );
    if let Some(status) = status(state) {
        let inset = if state.narrow { 19.0 } else { 28.0 };
        main = main.child(col().w_full().px(inset).py(10.0).child(status));
    }
    let screen = row()
        .size(0.0, 0.0)
        .w_full()
        .h_full()
        .fill(ink(BG))
        .child(sidebar(state))
        .child(main);
    let root = col()
        .key("intermux")
        .w_full()
        .h_full()
        .on_layout_tx(|s: &mut State, [_, _, width, _], tx| {
            let narrow = width < 1030.0;
            if s.narrow == narrow {
                tx.live_only();
            } else {
                s.narrow = narrow;
            }
        })
        .child(screen);
    if state.dialog.is_some() {
        root.child(dialogs::overlay(state))
    } else {
        root
    }
}

pub fn on_key(state: &mut State, event: &KeyEvent, tx: &mut Tx) -> bool {
    if event.key == Key::Escape && state.page == Page::Compose && state.dialog.is_none() {
        tx.instant();
        state.change_page(Page::Routes);
        tx.focus("route-app", true);
        return true;
    }
    false
}
