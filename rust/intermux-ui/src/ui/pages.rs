use super::dashboard::{app_icon, kind, route_color};
use super::*;
use crate::model::{App, Dialog, Job};
use rune::FileDialogOptions;

fn section(title: &str, detail: &str) -> El {
    col()
        .gap(4.0)
        .child(copy(title, 18.0, 700.0, TEXT))
        .child(copy(detail, 13.0, 420.0, MUTED).wrap())
}

fn choose_app(state: &State, app: &App, index: usize) -> El {
    let enabled = app.bindable;
    let selected = state
        .composer
        .app
        .as_ref()
        .is_some_and(|a| a.path == app.path);
    let detail = if app.bindable {
        if app.description.is_empty() {
            app.path.as_str()
        } else {
            app.description.as_str()
        }
    } else {
        app.reason
            .as_deref()
            .filter(|reason| !reason.is_empty())
            .unwrap_or("This app cannot be routed")
    };
    let entry = app.clone();
    let row = row()
        .key(format!("choose-app-{index}"))
        .h(45.0)
        .w_full()
        .px(8.0)
        .gap(9.0)
        .center_y()
        .radius(5.0)
        .fill(ink(if selected { ACTIVE } else { PANEL }))
        .hover(ink(HOVER))
        .child(app_icon(state, &app.path, FOCUS, 29.0))
        .child(
            col()
                .gap(2.0)
                .grow(1.0)
                .min_w(0.0)
                .child(
                    copy(
                        app.name.clone(),
                        14.0,
                        650.0,
                        if enabled { TEXT } else { MUTED },
                    )
                    .truncate(),
                )
                .child(copy(detail, 11.5, 500.0, FAINT).truncate()),
        )
        .child(if selected {
            copy("✓", 17.0, 650.0, FOCUS)
        } else if app.recent {
            copy("Recent", 11.0, 550.0, FAINT)
        } else {
            el()
        });
    let row = pressable(row, &format!("{}. {detail}", app.name), enabled);
    if enabled {
        row.on_press(move |s: &mut State| {
            s.choose_app(entry.clone());
        })
    } else {
        row
    }
}

fn browse_app() -> El {
    button_tx("app-browse", "Browse…", Button::Quiet, true, |_, tx| {
        let options = FileDialogOptions {
            title: "Choose an executable".into(),
            ..Default::default()
        };
        tx.open_file(options, |s: &mut State, answer, _| {
            if s.page != Page::Compose {
                return;
            }
            match answer {
                Ok(Some(path)) => {
                    s.composer.manual = true;
                    s.composer.path.text = path.to_string_lossy().into_owned();
                    s.form_error = None;
                }
                Ok(None) => {}
                Err(_) => {
                    s.composer.manual = true;
                    s.form_error = Some(
                        "The file picker is unavailable. Enter the executable path below.".into(),
                    );
                }
            }
        });
    })
}

fn app_picker(state: &State) -> El {
    let mut card =
        panel()
            .w_full()
            .padding(15.0)
            .gap(11.0)
            .child(copy("Application", 17.0, 690.0, TEXT));
    if state.composer.manual {
        return card
            .child(field(
                "app-path",
                "Executable",
                "Firefox or /usr/bin/firefox",
                &state.composer.path,
                |s| &mut s.composer.path,
            ))
            .child(row().gap(8.0).child(browse_app()).child(button_tx(
                "app-use-list",
                "Choose installed app",
                Button::Quiet,
                true,
                |s, tx| {
                    s.composer.manual = false;
                    s.form_error = None;
                    tx.focus("app-search", true);
                },
            )));
    }
    card = card.child(field(
        "app-search",
        "Search installed apps",
        "Search by name or executable…",
        &state.composer.search,
        |s| &mut s.composer.search,
    ));
    let query = state.composer.search.text.trim().to_lowercase();
    let mut list = col().w_full().gap(4.0);
    let matches: Vec<_> = state
        .apps
        .iter()
        .enumerate()
        .filter(|(_, app)| {
            query.is_empty()
                || app.name.to_lowercase().contains(&query)
                || app.path.to_lowercase().contains(&query)
        })
        .collect();
    if matches.is_empty() {
        list = list.child(
            copy(
                if state.apps_loaded {
                    "No matching apps. Try another name or enter a path."
                } else {
                    "Looking for installed apps…"
                },
                13.0,
                450.0,
                MUTED,
            )
            .wrap()
            .padding(10.0),
        );
    } else {
        for (index, app) in matches {
            list = list.child(choose_app(state, app, index));
        }
    }
    card.child(
        scroll(list)
            .h(if state.narrow { 201.0 } else { 297.0 })
            .key("app-options"),
    )
    .child(
        row()
            .gap(8.0)
            .child(button_tx(
                "app-enter-path",
                "Enter path",
                Button::Quiet,
                true,
                |s, tx| {
                    s.composer.manual = true;
                    s.form_error = None;
                    tx.focus("app-path", true);
                },
            ))
            .child(browse_app()),
    )
}

fn connection_picker(state: &State) -> El {
    let mut card =
        panel()
            .w_full()
            .padding(15.0)
            .gap(9.0)
            .child(copy("Network interface", 17.0, 690.0, TEXT));
    if state.interfaces().is_empty() {
        card = card.child(
            copy(
                "Connect to a network before creating a route.",
                13.0,
                450.0,
                MUTED,
            )
            .wrap(),
        );
    }
    for iface in state.interfaces() {
        let name = iface.name.clone();
        let selected = name == state.composer.iface;
        let enabled = iface.bindable && iface.state == "up";
        let color = route_color(iface);
        let label = format!("{}: {name}", kind(iface));
        let choice = pressable(
            row()
                .key(format!("compose-interface-{name}"))
                .w_full()
                .h(42.0)
                .px(8.0)
                .gap(8.0)
                .center_y()
                .radius(5.0)
                .fill(ink(if selected { ACTIVE } else { PANEL }))
                .hover(ink(HOVER))
                .child(el().size(8.0, 8.0).radius(2.0).fill(ink(if enabled {
                    color
                } else {
                    FAINT
                })))
                .child(copy(
                    kind(iface),
                    14.0,
                    630.0,
                    if enabled { TEXT } else { MUTED },
                ))
                .child(mono(name.clone(), 12.0, MUTED).truncate().min_w(0.0))
                .child(spacer())
                .child(copy(
                    if selected {
                        "Selected"
                    } else if enabled {
                        ""
                    } else {
                        "Unavailable"
                    },
                    11.0,
                    600.0,
                    if selected { FOCUS } else { FAINT },
                )),
            &label,
            enabled,
        );
        card = card.child(if enabled {
            choice.on_press(move |s: &mut State| {
                s.composer.iface.clone_from(&name);
                s.composer.interface_chosen = true;
                s.form_error = None;
            })
        } else {
            choice
        });
    }
    card
}

fn advanced(state: &State) -> El {
    let label = if state.composer.advanced {
        "Hide advanced settings"
    } else {
        "Advanced settings"
    };
    let control = button("advanced", label, Button::Quiet, true, |s| {
        s.composer.advanced = !s.composer.advanced
    });
    if !state.composer.advanced {
        return row().child(control);
    }
    panel()
        .w_full()
        .padding(15.0)
        .gap(12.0)
        .child(row().child(control))
        .child(field(
            "compose-dns",
            "DNS servers",
            "Keep current, enter a preset, or enter IPv4 addresses",
            &state.composer.dns,
            |s| &mut s.composer.dns,
        ))
        .child(
            row()
                .gap(6.0)
                .flex_wrap()
                .child(button("dns-auto", "Automatic", Button::Quiet, true, |s| {
                    s.composer.dns.text = "auto".into()
                }))
                .child(button(
                    "dns-cloudflare",
                    "Cloudflare",
                    Button::Quiet,
                    true,
                    |s| s.composer.dns.text = "cloudflare".into(),
                ))
                .child(button("dns-google", "Google", Button::Quiet, true, |s| {
                    s.composer.dns.text = "google".into()
                }))
                .child(button("dns-quad9", "Quad9", Button::Quiet, true, |s| {
                    s.composer.dns.text = "quad9".into()
                })),
        )
        .child(
            row()
                .gap(9.0)
                .child(
                    field(
                        "compose-limit-down",
                        "Download limit",
                        "Mbit/s",
                        &state.composer.down,
                        |s| &mut s.composer.down,
                    )
                    .grow(1.0),
                )
                .child(
                    field(
                        "compose-limit-up",
                        "Upload limit",
                        "Mbit/s",
                        &state.composer.up,
                        |s| &mut s.composer.up,
                    )
                    .grow(1.0),
                ),
        )
        .child(copy("If the app is running", 13.0, 650.0, TEXT))
        .child(
            row()
                .gap(7.0)
                .flex_wrap()
                .child(policy(state, "ask", "Ask each time"))
                .child(policy(state, "close", "Close and reopen"))
                .child(policy(state, "separate", "Separate profile")),
        )
}

fn policy(state: &State, code: &'static str, label: &str) -> El {
    let selected = state.composer.policy == code;
    button(
        format!("policy-{code}"),
        label,
        if selected {
            Button::Primary
        } else {
            Button::Quiet
        },
        true,
        move |s| s.composer.policy = code,
    )
    .role("radio")
    .checked(selected)
}

pub(super) fn composer(state: &State) -> El {
    let busy = state.busy().is_some();
    let setup = col()
        .w_full()
        .gap(11.0)
        .grow(1.0)
        .min_w(0.0)
        .child(connection_picker(state))
        .child(advanced(state));
    let pickers = if state.narrow {
        col().gap(13.0).child(app_picker(state)).child(setup)
    } else {
        el().equal_columns(2)
            .gap(13.0)
            .w_full()
            .items_start()
            .child(app_picker(state).min_w(0.0))
            .child(setup)
    };
    let mut content = col().w_full().gap(13.0).child(pickers);
    if let Some(error) = &state.form_error {
        content = content.child(copy(error.clone(), 13.0, 600.0, AMBER).role("alert").wrap());
    }
    content.child(
        row()
            .gap(8.0)
            .center_y()
            .child(button_tx(
                "cancel-compose",
                "Back to routes",
                Button::Quiet,
                !busy,
                |s, tx| {
                    s.change_page(Page::Routes);
                    tx.focus("route-app", true);
                },
            ))
            .child(spacer())
            .child(button(
                "queue-app",
                "Add to queue",
                Button::Quiet,
                !busy,
                |s| s.submit(false),
            ))
            .child(button(
                "start-app",
                "Route now",
                Button::Primary,
                !busy,
                |s| s.submit(true),
            )),
    )
}

fn queue_row(state: &State, job: &Job) -> El {
    let id = job.id;
    let pending = job.outcome.is_none();
    let idle = state.busy().is_none();
    let policy = match job.policy.as_str() {
        "close" => "Close and reopen",
        "separate" => "Separate profile",
        "shift" => "Shift running session",
        _ => "Ask if already running",
    };
    let mut content = col()
        .gap(4.0)
        .min_w(0.0)
        .grow(1.0)
        .child(copy(job.app.name.clone(), 15.0, 690.0, TEXT).truncate())
        .child(mono(format!("{}  ·  {policy}", job.iface), 11.0, MUTED).wrap());
    if let Some(issue) = &job.issue {
        content = content.child(copy(issue.clone(), 12.0, 600.0, AMBER).wrap());
    }
    if let Some(outcome) = &job.outcome {
        content = content.child(copy(outcome.clone(), 12.0, 600.0, GREEN).wrap());
    }
    let color = state
        .interfaces()
        .iter()
        .find(|i| i.name == job.iface)
        .map_or(FOCUS, route_color);
    let mut actions = row().gap(6.0).center_y();
    if pending {
        actions = actions.child(button(
            format!("start-job-{id}"),
            if job.issue.is_some() {
                "Retry"
            } else {
                "Start"
            },
            Button::Quiet,
            idle,
            move |s| s.start(vec![id]),
        ));
    }
    actions = actions.child(button(
        format!("remove-job-{id}"),
        if pending { "Remove" } else { "Dismiss" },
        Button::Quiet,
        idle,
        move |s| s.queue.retain(|job| job.id != id),
    ));
    row()
        .key(format!("job-{id}"))
        .w_full()
        .gap(10.0)
        .center_y()
        .padding(12.0)
        .child(app_icon(state, &job.app.path, color, 34.0))
        .child(content)
        .child(actions)
}

pub(super) fn queue(state: &State) -> El {
    let ids: Vec<_> = state
        .queue
        .iter()
        .filter(|job| job.outcome.is_none())
        .map(|job| job.id)
        .collect();
    let pending = ids.len();
    let idle = state.busy().is_none();
    let label = if state.snapshot.as_ref().is_some_and(|s| s.passwordless) {
        "Run pending launches together."
    } else {
        "Starting the queue uses one authorization prompt."
    };
    let mut list = panel().w_full().clip_rounded(8.0);
    for (index, job) in state.queue.iter().enumerate() {
        if index != 0 {
            list = list.child(rule());
        }
        list = list.child(queue_row(state, job));
    }
    if state.queue.is_empty() {
        list = list.child(
            section(
                "Queue is empty",
                "Add apps to the queue to launch them together.",
            )
            .padding(19.0),
        );
    }
    col()
        .w_full()
        .gap(12.0)
        .child(
            row()
                .gap(8.0)
                .center_y()
                .flex_wrap()
                .child(copy(format!("{pending} pending"), 16.0, 700.0, TEXT))
                .child(spacer())
                .child(button_tx(
                    "queue-add-more",
                    "Route another app",
                    Button::Quiet,
                    idle,
                    open_composer,
                ))
                .child(button(
                    "start-queue",
                    "Start all",
                    Button::Primary,
                    idle && pending > 0,
                    move |s| s.start(ids.clone()),
                )),
        )
        .child(list)
        .child(copy(label, 13.0, 450.0, MUTED).wrap())
}

pub(super) fn vpn(state: &State) -> El {
    let available = state.snapshot.as_ref().is_some_and(|s| s.vpn_available);
    let idle = state.busy().is_none();
    let mut list = panel().w_full().clip_rounded(8.0);
    if !available {
        list = list.child(
            section(
                "NetworkManager unavailable",
                "Bring the VPN up with its own tool. Its interface will appear in Routes.",
            )
            .padding(17.0),
        );
    } else if state.vpns.is_empty() {
        let title = if state.vpn_loaded {
            "No saved VPN connections"
        } else {
            "Reading VPN connections…"
        };
        list = list.child(
            section(
                title,
                "Import a WireGuard .conf or OpenVPN .ovpn profile to start.",
            )
            .padding(17.0),
        );
    } else {
        for (index, vpn) in state.vpns.iter().enumerate() {
            if index != 0 {
                list = list.child(rule());
            }
            let uuid = vpn.uuid.clone();
            let active = vpn.active;
            let kind = if vpn.kind == "wireguard" {
                "WireGuard"
            } else {
                "VPN"
            };
            let device = vpn.device.as_deref().filter(|name| !name.is_empty());
            let detail =
                device.map_or_else(|| kind.to_string(), |name| format!("{kind}  /  {name}"));
            list = list.child(
                row()
                    .key(format!("vpn-{}", vpn.uuid))
                    .w_full()
                    .center_y()
                    .gap(11.0)
                    .padding(14.0)
                    .child(
                        el().size(34.0, 34.0)
                            .center()
                            .radius(6.0)
                            .fill(ink(VIOLET).alpha(0.12))
                            .child(mono("◇", 22.0, VIOLET)),
                    )
                    .child(
                        col()
                            .gap(3.0)
                            .grow(1.0)
                            .min_w(0.0)
                            .child(copy(vpn.name.clone(), 16.0, 680.0, TEXT).truncate())
                            .child(mono(detail, 11.0, MUTED).truncate()),
                    )
                    .child(badge(
                        if active { "Connected" } else { "Disconnected" },
                        if active { GREEN } else { FAINT },
                    ))
                    .child(button(
                        format!("vpn-action-{}", vpn.uuid),
                        if active { "Disconnect" } else { "Connect" },
                        if active {
                            Button::Quiet
                        } else {
                            Button::Primary
                        },
                        idle,
                        move |s| {
                            if active {
                                s.request("vpn_disconnect", json!({"uuid": uuid}));
                            } else {
                                s.request(
                                    "vpn_connect",
                                    json!({"uuid": uuid, "apps_only": s.apps_only}),
                                );
                            }
                        },
                    )),
            );
        }
    }
    col()
        .w_full()
        .gap(14.0)
        .child(
            row()
                .center_y()
                .gap(8.0)
                .child(copy("Saved profiles", 17.0, 690.0, TEXT))
                .child(spacer())
                .child(button(
                    "vpn-refresh",
                    "Refresh",
                    Button::Quiet,
                    available && idle,
                    |s| s.request("vpn_list", json!({})),
                ))
                .child(button(
                    "vpn-import",
                    "Import VPN…",
                    Button::Primary,
                    available && idle,
                    |s| {
                        s.field = TextEdit::default();
                        s.form_error = None;
                        s.dialog = Some(Dialog::Import);
                    },
                )),
        )
        .child(list)
        .child(
            panel()
                .padding(14.0)
                .gap(7.0)
                .child(
                    pressable(
                        row()
                            .key("vpn-apps-only")
                            .center_y()
                            .gap(8.0)
                            .child(
                                el().size(18.0, 18.0)
                                    .radius(4.0)
                                    .center()
                                    .border(1.0, ink(BORDER))
                                    .fill(ink(if state.apps_only { ACCENT } else { BG }))
                                    .child(copy(
                                        if state.apps_only { "✓" } else { "" },
                                        12.0,
                                        700.0,
                                        TEXT,
                                    )),
                            )
                            .child(copy(
                                "Use VPN only for apps I route through it",
                                14.0,
                                650.0,
                                TEXT,
                            )),
                        "Use VPN only for apps I route through it",
                        available && idle,
                    )
                    .role("checkbox")
                    .checked(state.apps_only)
                    .on_press(|s: &mut State| {
                        if s.busy().is_none() {
                            s.apps_only = !s.apps_only;
                        }
                    }),
                )
                .child(
                    copy(
                        "Keeps your normal system route when connecting the VPN.",
                        13.0,
                        440.0,
                        MUTED,
                    )
                    .wrap(),
                ),
        )
}

pub(super) fn settings(state: &State) -> El {
    let idle = state.busy().is_none();
    let passwordless = state.snapshot.as_ref().is_some_and(|s| s.passwordless);
    let mut diagnostics = panel()
        .padding(15.0)
        .gap(11.0)
        .child(section(
            "Diagnostics",
            "Save a local report with logs and system details. You decide whether to share it.",
        ))
        .child(
            row()
                .gap(8.0)
                .flex_wrap()
                .child(button(
                    "save-report",
                    "Save report",
                    Button::Quiet,
                    idle,
                    |s| s.request("save_report", json!({})),
                ))
                .child(button(
                    "report-portal",
                    "Open reporting portal",
                    Button::Quiet,
                    idle,
                    |s| s.request("open_report", json!({})),
                )),
        );
    if let Some(path) = &state.report {
        let saved = path.clone();
        diagnostics = diagnostics
            .child(mono(saved.clone(), 11.0, MUTED).wrap_anywhere())
            .child(row().child(button(
                "report-folder",
                "Open report folder",
                Button::Quiet,
                idle,
                move |s| s.request("open_report_folder", json!({"path": saved})),
            )));
    }
    col().w_full().gap(13.0)
        .child(panel().padding(15.0).gap(10.0)
            .child(section("Authorization", "By default, InterMux asks for an administrator password before changing routes."))
            .child(row().gap(9.0).center_y().flex_wrap()
                .child(badge(if passwordless { "Passwordless enabled" } else { "Password required" },
                    if passwordless { AMBER } else { GREEN }))
                .child(button("change-passwordless", if passwordless { "Require password" } else { "Enable passwordless…" },
                    Button::Quiet, idle && state.snapshot.is_some(), move |s| {
                        let (title, detail, button) = if passwordless {
                            ("Require a password?", "InterMux will ask for an administrator password before making route changes.", "Require password")
                        } else {
                            ("Enable passwordless routing?", "Programs running as you will be able to change InterMux routes without a password. Changing this setting always requires an administrator password.", "Enable passwordless")
                        };
                        s.confirm(title, detail.into(), button, "passwordless", json!({"on": !passwordless}));
                    }))))
        .child(panel().padding(15.0).gap(11.0)
            .child(section("Route recovery", "Re-add routes after a connection returns, or remove routes with no running applications."))
            .child(row().gap(8.0).flex_wrap()
                .child(button("recover-routes", "Re-add routes", Button::Quiet, idle, |s| s.request("reconnect", json!({}))))
                .child(button("cleanup-routes", "Remove stopped", Button::Quiet, idle, |s| s.request("teardown_idle", json!({}))))
                .child(button("reset-routes", "Reset everything…", Button::Danger, idle, |s| {
                    s.reset_kill_apps = false;
                    s.confirm("Reset all InterMux routes?", "This removes InterMux namespaces, routes, firewall rules, and bindings. Routes in use may be kept if you leave their apps running.".into(),
                        "Reset routes", "reset", json!({"kill_apps": false}));
                }))))
        .child(diagnostics)
}
