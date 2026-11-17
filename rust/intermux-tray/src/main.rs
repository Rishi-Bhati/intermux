mod autostart;
mod daemon;
mod format;
mod helper;
mod model;
mod notify;
mod tray;

use ksni::TrayMethods;
use model::{Action, Snapshot};
use nix::fcntl::{Flock, FlockArg};
use std::{
    collections::BTreeSet,
    fs,
    os::unix::fs::DirBuilderExt,
    path::PathBuf,
    time::{Duration, Instant},
};
use tokio::{
    signal::unix::{signal, SignalKind},
    sync::{mpsc, oneshot},
};
use tray::UiEvent;

const NO_TRAY: &str =
    "Your desktop has no tray area. InterMux keeps running in the background and will \
notify you about connection changes. Open InterMux from the app menu.";

fn main() {
    let mut args = std::env::args().skip(1);
    match args.next().as_deref() {
        None => (),
        Some("--version") => {
            println!("intermux-tray {}", model::VERSION);
            return;
        }
        Some(_) => {
            eprintln!("usage: intermux-tray [--version]");
            std::process::exit(2);
        }
    }
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    if nix::unistd::getuid().is_root() {
        log::error!("intermux-tray must not run as root");
        std::process::exit(1);
    }
    let _lock = match lock() {
        Ok(Some(lock)) => lock,
        Ok(None) => {
            log::info!("another intermux-tray is running");
            return;
        }
        Err(e) => {
            log::error!("cannot take the tray lock: {e}");
            std::process::exit(1);
        }
    };
    let runtime = tokio::runtime::Runtime::new().expect("tokio runtime");
    let result = runtime.block_on(run());
    // Do not wait for a pending helper call (pkexec prompt) on the way out.
    runtime.shutdown_timeout(Duration::from_secs(1));
    if let Err(e) = result {
        log::error!("{e}");
        std::process::exit(1);
    }
}

/// `$XDG_RUNTIME_DIR/intermux/tray.lock`; `None` when another instance holds it.
fn lock() -> std::io::Result<Option<Flock<fs::File>>> {
    let runtime = std::env::var_os("XDG_RUNTIME_DIR")
        .filter(|s| !s.is_empty())
        .ok_or_else(|| std::io::Error::other("XDG_RUNTIME_DIR is unset"))?;
    let dir = PathBuf::from(runtime).join("intermux");
    match fs::DirBuilder::new().mode(0o700).create(&dir) {
        Err(e) if e.kind() != std::io::ErrorKind::AlreadyExists => return Err(e),
        _ => (),
    }
    let file = fs::OpenOptions::new()
        .create(true)
        .truncate(false)
        .write(true)
        .open(dir.join("tray.lock"))?;
    match Flock::lock(file, FlockArg::LockExclusiveNonblock) {
        Ok(lock) => Ok(Some(lock)),
        Err((_, nix::errno::Errno::EWOULDBLOCK)) => Ok(None),
        Err((_, e)) => Err(e.into()),
    }
}

fn open_window() {
    let program = ["/usr/bin/intermux"]
        .iter()
        .map(PathBuf::from)
        .find(|p| p.is_file())
        .unwrap_or_else(|| "intermux".into());
    if let Err(e) = daemon::spawn_detached(&program) {
        log::warn!("cannot start {}: {e}", program.display());
    }
}

struct App {
    uid: i64,
    snap: Snapshot,
    busy: BTreeSet<String>,
    autostart_path: Option<PathBuf>,
    autostart: bool,
    tray: Option<ksni::Handle<tray::Tray>>,
    notifier: notify::Notifier,
    no_tray_sent: bool,
    limiter: model::RateLimiter,
}

impl App {
    fn view(&self) -> model::View {
        model::build(&self.snap, self.uid, &self.busy, self.autostart)
    }
    async fn refresh(&self) {
        if let Some(handle) = &self.tray {
            let view = self.view();
            handle
                .update(|t| {
                    if t.view != view {
                        t.view = view;
                    }
                })
                .await;
        }
    }
    fn no_tray(&mut self) {
        if !std::mem::replace(&mut self.no_tray_sent, true) {
            self.notifier.send("InterMux".into(), NO_TRAY.into());
        }
    }
}

async fn run() -> std::io::Result<()> {
    let socket =
        daemon::socket_path().ok_or_else(|| std::io::Error::other("XDG_RUNTIME_DIR is unset"))?;
    let (ui_tx, mut ui_rx) = mpsc::unbounded_channel();
    let (update_tx, mut update_rx) = mpsc::channel(64);
    let (command_tx, command_rx) = mpsc::channel(8);
    let (done_tx, mut done_rx) = mpsc::unbounded_channel();
    let autostart_path = autostart::path();
    let mut app = App {
        uid: i64::from(nix::unistd::getuid().as_raw()),
        snap: Snapshot::default(),
        busy: BTreeSet::new(),
        autostart: autostart_path.as_deref().is_some_and(autostart::is_enabled),
        autostart_path,
        tray: None,
        notifier: notify::Notifier::new().await,
        no_tray_sent: false,
        limiter: model::RateLimiter::new(Duration::from_secs(10)),
    };

    let watcher = app.notifier.has_watcher().await;
    if !watcher {
        log::warn!("no StatusNotifierWatcher on the session bus; running without a tray icon");
        app.no_tray();
    }
    let ui = tray::Tray {
        view: app.view(),
        tx: ui_tx.clone(),
    };
    match ui.assume_sni_available(true).spawn().await {
        Ok(handle) => {
            if watcher {
                log::info!("registered with the StatusNotifierWatcher");
            }
            app.tray = Some(handle);
        }
        Err(e) => {
            log::warn!("cannot create the tray icon: {e}");
            app.no_tray();
        }
    }

    daemon::ensure(&socket).await;
    tokio::spawn(daemon::run(socket, update_tx, command_rx));

    let mut sigterm = signal(SignalKind::terminate())?;
    let mut sigint = signal(SignalKind::interrupt())?;
    loop {
        tokio::select! {
            _ = sigterm.recv() => break,
            _ = sigint.recv() => break,
            Some(update) = update_rx.recv() => {
                match update {
                    daemon::Update::Connected => app.snap.connected = true,
                    daemon::Update::Disconnected => {
                        if app.snap.connected {
                            log::info!("disconnected from the daemon");
                        }
                        app.snap = Snapshot::default();
                    }
                    daemon::Update::Interfaces(list) => app.snap.interfaces = list,
                    daemon::Update::Bindings(b, a) => (app.snap.bindings, app.snap.adoptions) = (b, a),
                    daemon::Update::Traffic(t) => app.snap.traffic = t,
                    daemon::Update::Event(event, data) => {
                        if let Some((iface, text)) = model::notification(&event, &data, &app.snap, app.uid) {
                            if app.limiter.allow(&iface, Instant::now()) {
                                app.notifier.send(text, String::new());
                            }
                        }
                    }
                }
                app.refresh().await;
            }
            Some(event) = ui_rx.recv() => match event {
                UiEvent::NoTray => app.no_tray(),
                UiEvent::Action(Action::Quit) => break,
                UiEvent::Action(Action::OpenGui) => {
                    let (reply, answer) = oneshot::channel();
                    let _ = command_tx.try_send(daemon::Command::RaiseGui(reply));
                    tokio::spawn(async move {
                        let raised = tokio::time::timeout(Duration::from_secs(2), answer).await;
                        if !matches!(raised, Ok(Ok(Some(true)))) {
                            open_window();
                        }
                    });
                }
                UiEvent::Action(Action::ToggleAutostart) => {
                    if let Some(path) = &app.autostart_path {
                        if let Err(e) = autostart::set(path, !app.autostart) {
                            app.notifier.send("InterMux: could not change Start at sign-in".into(), e.to_string());
                        }
                        app.autostart = autostart::is_enabled(path);
                    }
                    app.refresh().await;
                }
                UiEvent::Action(action) => {
                    let Some(iface) = helper::busy_key(&action).map(str::to_string) else { continue };
                    if !app.busy.insert(iface.clone()) {
                        continue;
                    }
                    let env = helper::filter_env(std::env::vars());
                    let request = helper::request(&action, &env).expect("helper action");
                    let done = done_tx.clone();
                    tokio::task::spawn_blocking(move || {
                        let result = helper::call(&request, helper::spawn);
                        let _ = done.send((iface, action, result));
                    });
                    app.refresh().await;
                }
            },
            Some((iface, action, result)) = done_rx.recv() => {
                app.busy.remove(&iface);
                if let Err(message) = result {
                    log::warn!("{}: {message}", helper::failure_summary(&action));
                    app.notifier.send(helper::failure_summary(&action), message);
                }
                app.refresh().await;
            }
        }
    }
    if let Some(handle) = app.tray.take() {
        handle.shutdown().await;
    }
    Ok(())
}
