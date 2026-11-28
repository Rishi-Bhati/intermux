use super::dashboard::kind;
use super::*;
use crate::model::{Dialog, Selection};
use rune::FileDialogOptions;

fn dismiss(state: &mut State) {
    if let Some(Dialog::Running { id, .. }) = state.dialog {
        state.running_choice(id, None);
    } else {
        state.dialog = None;
    }
    state.form_error = None;
}

fn cancel() -> El {
    button("dialog-cancel", "Cancel", Button::Quiet, true, dismiss).autofocus()
}

fn title(label: &str, detail: impl Into<String>) -> El {
    col()
        .gap(6.0)
        .child(copy(label, 24.0, 700.0, TEXT).role("heading"))
        .child(copy(detail, 14.0, 450.0, MUTED).wrap())
}

fn form_error(state: &State) -> El {
    state
        .form_error
        .as_ref()
        .map_or_else(el, |error| copy(error.clone(), 13.0, 550.0, AMBER).wrap())
}

fn import_browse() -> El {
    button_tx("vpn-browse", "Browse…", Button::Quiet, true, |_, tx| {
        let options = FileDialogOptions {
            title: "Choose a VPN configuration".into(),
            ..Default::default()
        }
        .filter("VPN configuration", &["conf", "ovpn"]);
        tx.open_file(options, |s: &mut State, answer, _| {
            if !matches!(s.dialog, Some(Dialog::Import)) {
                return;
            }
            match answer {
                Ok(Some(path)) => {
                    s.field.text = path.to_string_lossy().into_owned();
                    s.form_error = None;
                }
                Ok(None) => {}
                Err(_) => {
                    s.form_error =
                        Some("The file picker is unavailable. Enter the configuration path.".into())
                }
            }
        });
    })
}

pub(super) fn overlay(state: &State) -> El {
    let Some(dialog) = state.dialog.as_ref() else {
        return el();
    };
    let mut card = panel()
        .w(490.0)
        .padding(22.0)
        .gap(16.0)
        .border(1.0, ink(BORDER))
        .role("dialog")
        .modal(false, dismiss);
    card = match dialog {
        Dialog::Confirm {
            title: prompt,
            detail,
            label,
            op,
            args,
        } => {
            let reset = *op == "reset";
            let danger = matches!(*op, "reset" | "kill_app" | "remove_namespace");
            let action = *op;
            let args = args.clone();
            let mut confirm = card.label(*prompt).child(title(prompt, detail.clone()));
            if reset {
                confirm = confirm.child(
                    button(
                        "reset-kill",
                        if state.reset_kill_apps {
                            "Also close routed apps: on"
                        } else {
                            "Also close routed apps: off"
                        },
                        Button::Quiet,
                        true,
                        |s| s.reset_kill_apps = !s.reset_kill_apps,
                    )
                    .role("checkbox")
                    .checked(state.reset_kill_apps),
                );
            }
            confirm.child(row().gap(8.0).justify_end().child(cancel()).child(button(
                "dialog-apply",
                label,
                if danger {
                    Button::Danger
                } else {
                    Button::Primary
                },
                true,
                move |s| {
                    let args = if reset {
                        json!({"kill_apps": s.reset_kill_apps})
                    } else {
                        args.clone()
                    };
                    s.dialog = None;
                    s.request(action, args);
                },
            )))
        }
        Dialog::Running { id, count } => {
            let id = *id;
            let name = state
                .queue
                .iter()
                .find(|job| job.id == id)
                .map_or("This application", |job| job.app.name.as_str());
            let plural = if *count == 1 { "process" } else { "processes" };
            card = card.label("Application already running")
                .child(title("Application already running", format!("{name} has {count} running {plural}. Choose how it should use the new connection.")));
            for (key, label, policy) in [
                ("running-close", "Close and reopen", "close"),
                ("running-separate", "Open separate profile", "separate"),
                ("running-shift", "Shift current session", "shift"),
            ] {
                card = card.child(button(key, label, Button::Quiet, true, move |s| {
                    s.running_choice(id, Some(policy))
                }));
            }
            card.child(row().justify_end().child(cancel()))
        }
        Dialog::Dns(iface) | Dialog::Limit(iface) => {
            let dns = matches!(dialog, Dialog::Dns(_));
            let iface = iface.clone();
            let label = if dns { "DNS servers" } else { "Speed limit" };
            let card = card.label(label).child(title(
                label,
                format!("These settings apply to routed apps that share the {iface} namespace."),
            ));
            let card = if dns {
                card.child(field(
                    "dns-servers",
                    "IPv4 nameservers",
                    "Automatic",
                    &state.field,
                    |s| &mut s.field,
                ))
            } else {
                card.child(
                    row()
                        .gap(10.0)
                        .child(
                            field(
                                "speed-down",
                                "Download limit",
                                "Mbit/s",
                                &state.field,
                                |s| &mut s.field,
                            )
                            .grow(1.0),
                        )
                        .child(
                            field("speed-up", "Upload limit", "Mbit/s", &state.field2, |s| {
                                &mut s.field2
                            })
                            .grow(1.0),
                        ),
                )
            };
            card.child(form_error(state))
                .child(row().gap(8.0).justify_end().child(cancel()).child(button(
                    "dialog-apply",
                    "Save changes",
                    Button::Primary,
                    true,
                    move |s| s.apply_interface(iface.clone(), dns),
                )))
        }
        Dialog::Move(app) => {
            let mut move_card = card.label("Move application").child(title(
                "Move application",
                format!("Choose a different interface for {}.", app.name),
            ));
            for iface in state
                .interfaces()
                .iter()
                .filter(|i| i.bindable && i.name != app.iface)
            {
                let to = iface.name.clone();
                let args = json!({"iface": app.iface, "app": app.app, "to": to});
                move_card = move_card.child(button(
                    format!("move-to-{to}"),
                    &format!("{}  /  {to}", kind(iface)),
                    Button::Quiet,
                    true,
                    move |s| {
                        s.dialog = None;
                        s.selection = Some(Selection::Interface(to.clone()));
                        s.request("move_app", args.clone());
                    },
                ));
            }
            move_card.child(row().justify_end().child(cancel()))
        }
        Dialog::Import => card
            .label("Import VPN")
            .child(title(
                "Import VPN",
                "Choose a WireGuard .conf or OpenVPN .ovpn file.",
            ))
            .child(field(
                "vpn-file",
                "Configuration file",
                "/path/to/profile.conf",
                &state.field,
                |s| &mut s.field,
            ))
            .child(row().child(import_browse()))
            .child(form_error(state))
            .child(row().gap(8.0).justify_end().child(cancel()).child(button(
                "dialog-apply",
                "Import",
                Button::Primary,
                true,
                |s| {
                    let path = s.field.text.trim().to_string();
                    if !std::path::Path::new(&path).is_absolute() {
                        s.form_error =
                            Some("Enter the absolute path to the VPN configuration.".into());
                    } else {
                        s.dialog = None;
                        s.request("vpn_import", json!({"path": path}));
                    }
                },
            ))),
    };
    el().key("modal-layer")
        .pin(Some(0.0), Some(0.0), Some(0.0), Some(0.0))
        .fill(Color::BLACK.alpha(0.74))
        .center()
        .child(card.key("modal"))
}
