//! The per-user "Start at sign-in" desktop entry.
use std::{ffi::OsString, fs, io, path::PathBuf};

pub const CONTENT: &str = "[Desktop Entry]
Type=Application
Name=InterMux tray
Exec=/usr/lib/intermux/intermux-tray
Icon=intermux
X-GNOME-Autostart-enabled=true
NoDisplay=true
";

/// `$XDG_CONFIG_HOME/autostart/intermux-tray.desktop`, with the XDG default `~/.config`.
pub fn path_from(config_home: Option<OsString>, home: Option<OsString>) -> Option<PathBuf> {
    let base = config_home
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
        .or_else(|| {
            home.map(PathBuf::from)
                .filter(|p| p.is_absolute())
                .map(|h| h.join(".config"))
        })?;
    Some(base.join("autostart").join("intermux-tray.desktop"))
}

pub fn path() -> Option<PathBuf> {
    path_from(
        std::env::var_os("XDG_CONFIG_HOME"),
        std::env::var_os("HOME"),
    )
}

pub fn is_enabled(path: &std::path::Path) -> bool {
    path.is_file()
}

pub fn set(path: &std::path::Path, on: bool) -> io::Result<()> {
    if on {
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir)?;
        }
        let tmp = path.with_extension("desktop.tmp");
        fs::write(&tmp, CONTENT)?;
        fs::rename(&tmp, path)
    } else {
        match fs::remove_file(path) {
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
            other => other,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn paths() {
        let p = |c: Option<&str>, h: Option<&str>| path_from(c.map(Into::into), h.map(Into::into));
        assert_eq!(
            p(Some("/c"), Some("/h")),
            Some("/c/autostart/intermux-tray.desktop".into())
        );
        assert_eq!(
            p(None, Some("/h")),
            Some("/h/.config/autostart/intermux-tray.desktop".into())
        );
        assert_eq!(
            p(Some("rel"), Some("/h")),
            Some("/h/.config/autostart/intermux-tray.desktop".into())
        );
        assert_eq!(p(None, None), None);
    }

    #[test]
    fn on_and_off() {
        let dir = std::env::temp_dir().join(format!("intermux-tray-test-{}", std::process::id()));
        let file = path_from(Some(dir.clone().into()), None).unwrap();
        assert!(!is_enabled(&file));
        set(&file, true).unwrap();
        assert!(is_enabled(&file));
        assert_eq!(fs::read_to_string(&file).unwrap(), CONTENT);
        set(&file, true).unwrap();
        set(&file, false).unwrap();
        assert!(!is_enabled(&file));
        set(&file, false).unwrap();
        fs::remove_dir_all(dir).unwrap();
    }
}
