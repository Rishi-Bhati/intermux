//! Text for rates, kinds and states, matching the Python window's wording.

const UNITS: [&str; 5] = ["B", "KB", "MB", "GB", "TB"];

/// Bytes/s as text, like `intermux/system/traffic.py: format_rate`.
pub fn rate(bytes_per_sec: f64) -> String {
    let mut value = if bytes_per_sec.is_finite() {
        bytes_per_sec.max(0.0)
    } else {
        0.0
    };
    let mut unit = 0;
    while value >= 1000.0 && unit < UNITS.len() - 1 {
        value /= 1024.0;
        unit += 1;
    }
    let number = if unit > 0 && value < 10.0 {
        format!("{value:.1}")
    } else {
        format!("{value:.0}")
    };
    format!("{number} {}/s", UNITS[unit])
}

pub fn kind_label(kind: &str) -> &'static str {
    match kind {
        "wifi" => "Wi-Fi",
        "ethernet" => "Ethernet",
        "usb" => "USB",
        "bluetooth" => "Bluetooth",
        "mobile" => "Mobile",
        "vpn" => "VPN",
        _ => "Other",
    }
}

/// The word shown instead of speeds; `None` while connected.
pub fn state_word(state: &str) -> Option<&'static str> {
    match state {
        "connected" => None,
        "no_route" => Some("No gateway"),
        "missing" => Some("Missing"),
        _ => Some("Disconnected"),
    }
}

/// Mbit/s without a trailing ".0".
pub fn mbit(value: f64) -> String {
    let text = format!("{value:.2}");
    text.trim_end_matches('0').trim_end_matches('.').to_string()
}

pub fn apps(count: usize) -> String {
    if count == 1 {
        "1 app".into()
    } else {
        format!("{count} apps")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rates() {
        assert_eq!(rate(0.0), "0 B/s");
        assert_eq!(rate(-5.0), "0 B/s");
        assert_eq!(rate(f64::NAN), "0 B/s");
        assert_eq!(rate(999.0), "999 B/s");
        assert_eq!(rate(1000.0), "1.0 KB/s");
        assert_eq!(rate(120.0 * 1024.0), "120 KB/s");
        assert_eq!(rate(9.94 * 1024.0), "9.9 KB/s");
        assert_eq!(rate(10.0 * 1024.0), "10 KB/s");
        assert_eq!(rate(4.1 * 1024.0 * 1024.0), "4.1 MB/s");
        assert_eq!(rate(999.0 * 1024.0), "999 KB/s");
        assert_eq!(rate(1000.0 * 1024.0), "1.0 MB/s");
        assert_eq!(rate(2.0 * 1024f64.powi(3)), "2.0 GB/s");
        assert_eq!(rate(5000.0 * 1024f64.powi(4)), "5000 TB/s");
    }

    #[test]
    fn labels() {
        assert_eq!(kind_label("wifi"), "Wi-Fi");
        assert_eq!(kind_label("virtual"), "Other");
        assert_eq!(state_word("connected"), None);
        assert_eq!(state_word("down"), Some("Disconnected"));
        assert_eq!(state_word("no_route"), Some("No gateway"));
        assert_eq!(state_word("missing"), Some("Missing"));
        assert_eq!(mbit(20.0), "20");
        assert_eq!(mbit(2.5), "2.5");
        assert_eq!(apps(1), "1 app");
        assert_eq!(apps(2), "2 apps");
    }
}
