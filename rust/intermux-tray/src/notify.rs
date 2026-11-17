//! Desktop notifications (org.freedesktop.Notifications) and the tray-watcher check, over zbus.
use std::collections::HashMap;
use zbus::{zvariant::Value, Connection};

#[derive(Clone)]
pub struct Notifier {
    conn: Option<Connection>,
}

impl Notifier {
    pub async fn new() -> Self {
        let conn = match Connection::session().await {
            Ok(c) => Some(c),
            Err(e) => {
                log::warn!("no session bus, notifications are off: {e}");
                None
            }
        };
        Self { conn }
    }

    /// Whether `org.kde.StatusNotifierWatcher` has an owner on the session bus.
    pub async fn has_watcher(&self) -> bool {
        let Some(conn) = &self.conn else { return false };
        let Ok(dbus) = zbus::fdo::DBusProxy::new(conn).await else {
            return false;
        };
        let Ok(name) = zbus::names::BusName::try_from("org.kde.StatusNotifierWatcher") else {
            return false;
        };
        dbus.name_has_owner(name).await.unwrap_or(false)
    }

    /// Sends in the background; failures are only logged.
    pub fn send(&self, summary: String, body: String) {
        log::info!("notification: {summary} {body}");
        let Some(conn) = self.conn.clone() else {
            return;
        };
        tokio::spawn(async move {
            let hints: HashMap<&str, Value> = HashMap::new();
            let result = conn
                .call_method(
                    Some("org.freedesktop.Notifications"),
                    "/org/freedesktop/Notifications",
                    Some("org.freedesktop.Notifications"),
                    "Notify",
                    &(
                        "InterMux",
                        0u32,
                        "intermux",
                        summary.as_str(),
                        body.as_str(),
                        Vec::<&str>::new(),
                        hints,
                        -1i32,
                    ),
                )
                .await;
            if let Err(e) = result {
                log::warn!("cannot send a notification: {e}");
            }
        });
    }
}
