use crate::state::Limit;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    net::Ipv4Addr,
    path::{Component, Path, PathBuf},
};

const VIRTUAL_PREFIXES: [&str; 7] = ["lo", "veth", "br-", "docker", "virbr", "vmnet", "vboxnet"];
const VPN_PREFIXES: [&str; 4] = ["tun", "tap", "wg", "ppp"];

pub fn is_virtual_name(name: &str) -> bool {
    VIRTUAL_PREFIXES.iter().any(|p| name.starts_with(p))
}

pub fn classify_kind(name: &str, base: &Path) -> String {
    let dev = base.join(name);
    let read = |file: &str| std::fs::read_to_string(dev.join(file)).unwrap_or_default();
    let tunnel = !name.starts_with("tailscale")
        && (dev.join("tun_flags").exists()
            || read("uevent")
                .split_whitespace()
                .any(|s| s == "DEVTYPE=wireguard")
            || read("type").trim() == "512");
    let kind = if is_virtual_name(name) {
        "virtual"
    } else if VPN_PREFIXES.iter().any(|p| name.starts_with(p)) || tunnel {
        "vpn"
    } else if dev.join("wireless").exists() || dev.join("phy80211").exists() {
        "wifi"
    } else if name.starts_with("wwan") {
        "mobile"
    } else if name.starts_with("bnep") {
        "bluetooth"
    } else if real_device_path(&dev.join("device")).contains("/usb") {
        "usb"
    } else if name.starts_with("en") || name.starts_with("eth") {
        "ethernet"
    } else {
        "other"
    };
    kind.into()
}

// Like Python realpath, also resolve a dangling sysfs device symlink (useful in fixtures).
fn real_device_path(path: &Path) -> String {
    if let Ok(real) = path.canonicalize() {
        return real.to_string_lossy().into_owned();
    }
    fn resolve(path: &Path, depth: usize) -> PathBuf {
        let path = if path.is_absolute() {
            path.to_owned()
        } else {
            std::env::current_dir()
                .unwrap_or_else(|_| PathBuf::from("/"))
                .join(path)
        };
        let mut resolved = PathBuf::new();
        for component in path.components() {
            match component {
                Component::CurDir => (),
                Component::ParentDir => {
                    resolved.pop();
                }
                _ => {
                    resolved.push(component);
                    if depth < 40 {
                        if let Ok(target) = std::fs::read_link(&resolved) {
                            resolved = resolve(
                                &resolved.parent().unwrap_or(Path::new("/")).join(target),
                                depth + 1,
                            );
                        }
                    }
                }
            }
        }
        resolved
    }
    resolve(path, 0).to_string_lossy().into_owned()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Address {
    pub family: String,
    pub local: String,
    pub prefixlen: i64,
    pub peer: Option<String>,
    pub scope: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Link {
    pub name: String,
    pub kind: String,
    pub up: bool,
    pub operstate: String,
    pub flags: Vec<String>,
    pub mac: Option<String>,
    pub mtu: Option<i64>,
    pub addresses: Vec<Address>,
    pub default_route: bool,
    pub gateways: Vec<String>,
}

impl Link {
    pub fn ipv4_cidrs(&self) -> Vec<String> {
        self.addresses
            .iter()
            .filter(|a| a.family == "inet")
            .map(|a| format!("{}/{}", a.local, a.prefixlen))
            .collect()
    }
    pub fn is_virtual(&self) -> bool {
        self.kind == "virtual" || is_virtual_name(&self.name)
    }
    pub fn bindable(&self) -> bool {
        self.up
            && !self.ipv4_cidrs().is_empty()
            && !self.is_virtual()
            && (self.default_route || self.kind == "vpn")
    }
}

pub(crate) fn text(value: &Value) -> Option<&str> {
    value.as_str().filter(|s| !s.is_empty())
}
pub(crate) fn integer(value: &Value) -> Option<i64> {
    if value.is_boolean() {
        return None;
    }
    value
        .as_i64()
        .or_else(|| value.as_str()?.trim().parse().ok())
        .or_else(|| {
            value
                .as_f64()
                .filter(|n| n.is_finite() && *n >= i64::MIN as f64 && *n < i64::MAX as f64)
                .map(|n| n as i64)
        })
}
fn items(value: &Value) -> impl Iterator<Item = &Value> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .filter(|v| v.is_object())
}

pub fn parse_ip_json(links: &Value, addrs: &Value, routes: &Value, base: &Path) -> Vec<Link> {
    let mut addresses: BTreeMap<String, Vec<Address>> = BTreeMap::new();
    for entry in items(addrs) {
        let Some(name) = text(&entry["ifname"]) else {
            continue;
        };
        for info in items(&entry["addr_info"]) {
            let Some(family @ ("inet" | "inet6")) = text(&info["family"]) else {
                continue;
            };
            let Some(local) = text(&info["local"]) else {
                continue;
            };
            addresses.entry(name.into()).or_default().push(Address {
                family: family.into(),
                local: local.into(),
                prefixlen: integer(&info["prefixlen"]).unwrap_or(if family == "inet" {
                    32
                } else {
                    128
                }),
                peer: text(&info["address"])
                    .filter(|p| *p != local)
                    .map(str::to_owned),
                scope: text(&info["scope"]).unwrap_or("").into(),
            });
        }
    }
    let mut default_devs = BTreeSet::new();
    let mut gateways: BTreeMap<String, Vec<String>> = BTreeMap::new();
    for route in items(routes) {
        if !matches!(text(&route["dst"]), Some("default" | "0.0.0.0/0")) {
            continue;
        }
        let Some(dev) = text(&route["dev"]) else {
            continue;
        };
        default_devs.insert(dev);
        if let Some(gw) = text(&route["gateway"]).filter(|s| s.parse::<Ipv4Addr>().is_ok()) {
            let via = gateways.entry(dev.into()).or_default();
            if !via.iter().any(|s| s == gw) {
                via.push(gw.into());
            }
        }
    }
    items(links)
        .filter_map(|link| {
            let name = text(&link["ifname"])?;
            if name == "lo" {
                return None;
            }
            let flags: Vec<String> = link["flags"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect();
            let operstate = text(&link["operstate"]).unwrap_or("UNKNOWN");
            Some(Link {
                name: name.into(),
                kind: classify_kind(name, base),
                up: flags.iter().any(|s| s == "UP")
                    && !matches!(operstate, "DOWN" | "LOWERLAYERDOWN"),
                operstate: operstate.into(),
                flags,
                mac: text(&link["address"]).map(str::to_owned),
                mtu: integer(&link["mtu"]),
                addresses: addresses.get(name).cloned().unwrap_or_default(),
                default_route: default_devs.contains(name),
                gateways: gateways.get(name).cloned().unwrap_or_default(),
            })
        })
        .collect()
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Interface {
    pub name: String,
    pub kind: String,
    pub up: bool,
    pub bindable: bool,
    pub ipv4: Vec<String>,
    pub gateway: Option<String>,
    pub state: String,
    pub routed: usize,
    pub link_speed: Option<f64>,
    pub dns: Vec<String>,
    pub limit: Option<Limit>,
}

impl Interface {
    pub fn status_changed(&self, other: &Self) -> bool {
        self.up != other.up
            || self.state != other.state
            || self.bindable != other.bindable
            || self.ipv4 != other.ipv4
            || self.gateway != other.gateway
            || self.routed != other.routed
    }
}
