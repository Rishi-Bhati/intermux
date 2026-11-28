mod bridge;
mod model;
mod ui;

use model::State;
use rune::{App, AppSpec};
use std::cell::RefCell;
use std::process::ExitCode;
use std::rc::Rc;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::Duration;

const DEFAULT_SIZE: (f32, f32) = (1240.0, 775.0);
const MIN_SIZE: (f32, f32) = (660.0, 480.0);
const WINDOW_UNAVAILABLE: u8 = 75;

fn main() -> ExitCode {
    // The native launcher is deliberately outside the privileged polkit executable.
    if unsafe { libc::getuid() == 0 || libc::geteuid() == 0 } {
        eprintln!("InterMux must run as a regular user.");
        return ExitCode::FAILURE;
    }
    rune::set_app_id("intermux");
    let headless = rune::is_headless();
    let preview = std::env::args().any(|arg| arg == "--scene" || arg.starts_with("--scene="));
    if !headless && !preview && !rune::single_instance("org.intermux.InterMux") {
        return ExitCode::SUCCESS;
    }

    let spec = AppSpec {
        min_size: Some(MIN_SIZE),
        scenes: ui::SCENES,
        fonts: &[
            include_bytes!("../fonts/SchibstedGrotesk.ttf"),
            include_bytes!("../fonts/JetBrainsMono.ttf"),
        ],
        families: ("Schibsted Grotesk", "JetBrains Mono"),
        setup: Some(State::set_up_images),
        on_key: Some(ui::on_key),
        ..AppSpec::new("InterMux", DEFAULT_SIZE, State::default, ui::view)
    };
    let app = App::new(spec);
    if headless || preview {
        return finish(app.try_run(), None, false);
    }

    let bridge = Rc::new(RefCell::new(None));
    let ready = Arc::new(AtomicBool::new(false));
    let handle = app.handle();
    let app = app.on_ready({
        let bridge = Rc::clone(&bridge);
        let ready = Arc::clone(&ready);
        let handle = handle.clone();
        move |_| {
            if !ready.swap(true, Ordering::AcqRel) {
                *bridge.borrow_mut() = Some(bridge::Bridge::start(handle.clone()));
            }
        }
    });
    // Rune can recover a lost surface, but it keeps running windowless on initial creation failure.
    let done = Arc::new(AtomicBool::new(false));
    let watchdog = thread::spawn({
        let handle = handle.clone();
        let done = Arc::clone(&done);
        let ready = Arc::clone(&ready);
        move || {
            while !done.load(Ordering::Acquire) && !ready.load(Ordering::Acquire) {
                if handle.window_error().is_some() {
                    handle.quit();
                    break;
                }
                thread::sleep(Duration::from_millis(50));
            }
        }
    });
    let result = app.try_run();
    done.store(true, Ordering::Release);
    let _ = watchdog.join();
    bridge.borrow_mut().take();
    finish(
        result,
        handle.window_error(),
        !ready.load(Ordering::Acquire),
    )
}

fn finish(result: Result<(), String>, window_error: Option<String>, startup: bool) -> ExitCode {
    if let Some(error) = result.err().or(window_error) {
        eprintln!("InterMux: {error}");
        return if startup {
            ExitCode::from(WINDOW_UNAVAILABLE)
        } else {
            ExitCode::FAILURE
        };
    }
    ExitCode::SUCCESS
}
