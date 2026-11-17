//! ksni glue: renders a `model::View` as a StatusNotifierItem.
use crate::model::{Action, IconState, Node, View};
use ksni::{menu, Icon, MenuItem, Status, ToolTip};
use tokio::sync::mpsc::UnboundedSender;

pub enum UiEvent {
    Action(Action),
    /// ksni could not reach a StatusNotifierWatcher.
    NoTray,
}

pub struct Tray {
    pub view: View,
    pub tx: UnboundedSender<UiEvent>,
}

/// A 16x16 amber dot, ARGB32 in network byte order.
fn attention_dot() -> Icon {
    let size = 16;
    let mut data = Vec::with_capacity(size * size * 4);
    for y in 0..size {
        for x in 0..size {
            let (dx, dy) = (x as f32 - 11.5, y as f32 - 11.5);
            let inside = dx * dx + dy * dy <= 16.0;
            data.extend_from_slice(if inside {
                &[255, 245, 158, 11]
            } else {
                &[0, 0, 0, 0]
            });
        }
    }
    Icon {
        width: size as i32,
        height: size as i32,
        data,
    }
}

fn send(action: &Action) -> Box<dyn Fn(&mut Tray) + Send> {
    let action = action.clone();
    Box::new(move |t: &mut Tray| {
        let _ = t.tx.send(UiEvent::Action(action.clone()));
    })
}

fn items(nodes: &[Node]) -> Vec<MenuItem<Tray>> {
    nodes
        .iter()
        .map(|node| match node {
            Node::Label(label) => menu::StandardItem {
                label: label.clone(),
                enabled: false,
                ..Default::default()
            }
            .into(),
            Node::Separator => MenuItem::Separator,
            Node::Item {
                label,
                enabled,
                action,
            } => menu::StandardItem {
                label: label.clone(),
                enabled: *enabled,
                activate: send(action),
                ..Default::default()
            }
            .into(),
            Node::Check {
                label,
                checked,
                action,
            } => menu::CheckmarkItem {
                label: label.clone(),
                checked: *checked,
                activate: send(action),
                ..Default::default()
            }
            .into(),
            Node::Sub { label, children } => menu::SubMenu {
                label: label.clone(),
                submenu: items(children),
                ..Default::default()
            }
            .into(),
            Node::Radio { selected, options } => {
                let (current, actions): (usize, Vec<_>) = (
                    *selected,
                    options.iter().map(|o| o.action.clone()).collect(),
                );
                menu::RadioGroup {
                    selected: *selected,
                    select: Box::new(move |t: &mut Tray, i| {
                        if i != current {
                            if let Some(Some(a)) = actions.get(i) {
                                let _ = t.tx.send(UiEvent::Action(a.clone()));
                            }
                        }
                    }),
                    options: options
                        .iter()
                        .map(|o| menu::RadioItem {
                            label: o.label.clone(),
                            enabled: o.enabled,
                            ..Default::default()
                        })
                        .collect(),
                }
                .into()
            }
        })
        .collect()
}

impl ksni::Tray for Tray {
    const MENU_ON_ACTIVATE: bool = true;

    fn id(&self) -> String {
        "intermux".into()
    }
    fn title(&self) -> String {
        self.view.title.clone()
    }
    fn icon_name(&self) -> String {
        "intermux".into()
    }
    fn attention_icon_name(&self) -> String {
        "intermux".into()
    }
    fn overlay_icon_pixmap(&self) -> Vec<Icon> {
        match self.view.icon {
            IconState::Attention => vec![attention_dot()],
            _ => vec![],
        }
    }
    fn status(&self) -> Status {
        // Idle stays Active: some hosts (GNOME AppIndicator) hide Passive items entirely.
        match self.view.icon {
            IconState::Attention => Status::NeedsAttention,
            IconState::Idle | IconState::Active => Status::Active,
        }
    }
    fn tool_tip(&self) -> ToolTip {
        ToolTip {
            icon_name: "intermux".into(),
            title: "InterMux".into(),
            description: self.view.tooltip.clone(),
            ..Default::default()
        }
    }
    fn menu(&self) -> Vec<MenuItem<Self>> {
        items(&self.view.menu)
    }
    fn watcher_offline(&self, reason: ksni::OfflineReason) -> bool {
        log::warn!("StatusNotifierWatcher offline: {reason:?}");
        if matches!(reason, ksni::OfflineReason::Error(_)) {
            let _ = self.tx.send(UiEvent::NoTray);
        }
        true
    }
    fn watcher_online(&self) {
        log::info!("StatusNotifierWatcher is back; registered again");
    }
}
