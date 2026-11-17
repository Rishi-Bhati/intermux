use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, path::Path};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Counters {
    pub rx: u64,
    pub tx: u64,
}

pub fn read_counters<'a>(
    names: impl IntoIterator<Item = &'a str>,
    base: &Path,
) -> BTreeMap<String, Counters> {
    names
        .into_iter()
        .filter_map(|name| {
            if name.is_empty() || name.contains('/') || matches!(name, "." | "..") {
                return None;
            }
            let stats = base.join(name).join("statistics");
            let read = |file| {
                std::fs::read_to_string(stats.join(file))
                    .ok()?
                    .trim()
                    .parse()
                    .ok()
            };
            Some((
                name.into(),
                Counters {
                    rx: read("rx_bytes")?,
                    tx: read("tx_bytes")?,
                },
            ))
        })
        .collect()
}

pub fn link_speed_bytes(name: &str, base: &Path) -> Option<f64> {
    let speed: u64 = std::fs::read_to_string(base.join(name).join("speed"))
        .ok()?
        .trim()
        .parse()
        .ok()?;
    (1..=1_000_000)
        .contains(&speed)
        .then_some(speed as f64 * 1_000_000.0 / 8.0)
}
pub fn iface_sample(c: Counters) -> (u64, u64) {
    (c.rx, c.tx)
}
pub fn veth_sample(c: Counters) -> (u64, u64) {
    (c.tx, c.rx)
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Rate {
    pub down: f64,
    pub up: f64,
}
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct TrafficSample {
    pub t: f64,
    pub interfaces: BTreeMap<String, Rate>,
    pub namespaces: BTreeMap<String, Rate>,
}

#[derive(Default)]
pub struct RateTracker {
    last: BTreeMap<String, (u64, u64)>,
    time: Option<std::time::Instant>,
}
impl RateTracker {
    pub fn update(
        &mut self,
        samples: BTreeMap<String, (u64, u64)>,
        now: std::time::Instant,
    ) -> BTreeMap<String, Rate> {
        let elapsed = self
            .time
            .map(|t| now.duration_since(t).as_secs_f64())
            .unwrap_or(0.0);
        let rates = samples
            .iter()
            .map(|(name, &(down, up))| {
                let rate = match self.last.get(name) {
                    Some(&(d, u)) if elapsed > 0.0 => Rate {
                        down: down.saturating_sub(d) as f64 / elapsed,
                        up: up.saturating_sub(u) as f64 / elapsed,
                    },
                    _ => Rate::default(),
                };
                (name.clone(), rate)
            })
            .collect();
        self.last = samples;
        self.time = Some(now);
        rates
    }
}
