use crate::config::Config;
use intermux_common::{
    interface::{classify_kind, Interface, Link},
    state::{AdoptionView, BindingView, State},
    traffic::{
        iface_sample, link_speed_bytes, read_counters, veth_sample, RateTracker, TrafficSample,
    },
};
use std::{
    collections::{BTreeMap, BTreeSet},
    io,
    os::unix::fs::MetadataExt,
    path::Path,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::process::Command;

#[derive(Debug, Clone, Default)]
pub struct View {
    pub interfaces: Vec<Interface>,
    pub bindings: Vec<BindingView>,
    pub adoptions: Vec<AdoptionView>,
}

pub async fn read_links(base: &Path) -> io::Result<Vec<Link>> {
    async fn ip(args: &[&str]) -> io::Result<serde_json::Value> {
        let mut command = Command::new("ip");
        // Clear activation variables only in the child; do not mutate this multithreaded
        // process's environment. The inherited listener also has FD_CLOEXEC set.
        command
            .args(args)
            .kill_on_drop(true)
            .env_remove("LISTEN_FDS")
            .env_remove("LISTEN_PID");
        let output = tokio::time::timeout(Duration::from_secs(2), command.output())
            .await
            .map_err(|_| io::Error::new(io::ErrorKind::TimedOut, "ip timed out"))??;
        if !output.status.success() {
            return Err(io::Error::other(format!(
                "ip failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            )));
        }
        serde_json::from_slice(&output.stdout)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
    }
    let (links, addrs, routes) = tokio::try_join!(
        ip(&["-j", "link"]),
        ip(&["-j", "addr"]),
        ip(&["-j", "route", "show", "table", "main"])
    )?;
    Ok(intermux_common::interface::parse_ip_json(
        &links, &addrs, &routes, base,
    ))
}

#[derive(Debug)]
struct Proc {
    pid: u32,
    uid: i64,
    netns: Option<u64>,
}
fn process(proc: &Path, pid: u32) -> Option<Proc> {
    let dir = proc.join(pid.to_string());
    let status = std::fs::read_to_string(dir.join("status")).ok()?;
    let mut uid = None;
    let mut ppid = None;
    for line in status.lines() {
        if let Some(state) = line.strip_prefix("State:") {
            if matches!(state.split_whitespace().next(), Some("Z" | "X")) {
                return None;
            }
        }
        if let Some(s) = line.strip_prefix("Uid:") {
            uid = s.split_whitespace().next().and_then(|s| s.parse().ok());
        }
        if let Some(s) = line.strip_prefix("PPid:") {
            ppid = s
                .split_whitespace()
                .next()
                .and_then(|s| s.parse::<u32>().ok());
        }
    }
    ppid?;
    // Python list_procs requires a readable comm as well as status.
    std::fs::read(dir.join("comm")).ok()?;
    Some(Proc {
        pid,
        uid: uid?,
        netns: std::fs::metadata(dir.join("ns/net")).ok().map(|m| m.ino()),
    })
}
pub fn build(state: &State, links: &[Link], config: &Config, netns: &Path, cgroups: &Path) -> View {
    let mut procs: Vec<Proc> = std::fs::read_dir(&config.proc)
        .into_iter()
        .flatten()
        .filter_map(|entry| {
            let pid = entry.ok()?.file_name().to_str()?.parse().ok()?;
            process(&config.proc, pid)
        })
        .collect();
    procs.sort_by_key(|p| p.pid);
    let mut namespaces: BTreeMap<String, Vec<u32>> = BTreeMap::new();
    let bindings: Vec<_> = state
        .bindings
        .iter()
        .map(|b| {
            let pids = namespaces
                .entry(b.namespace.clone())
                .or_insert_with(|| {
                    let inode = std::fs::metadata(netns.join(&b.namespace))
                        .ok()
                        .map(|m| m.ino());
                    procs
                        .iter()
                        .filter(|p| inode.is_some() && p.netns == inode)
                        .map(|p| p.pid)
                        .collect()
                })
                .clone();
            let uid = b.uid.or_else(|| {
                b.pid.and_then(|pid| {
                    procs
                        .iter()
                        .find(|p| i64::from(p.pid) == pid)
                        .map(|p| p.uid)
                })
            });
            BindingView {
                iface: b.iface.clone(),
                app: b.app.clone(),
                app_name: b.app_name.clone(),
                namespace: b.namespace.clone(),
                pid: b.pid,
                uid,
                running: !pids.is_empty(),
                pids,
                log_path: b.log_path.clone(),
            }
        })
        .collect();
    let adoptions: Vec<_> = state
        .adoptions
        .iter()
        .map(|a| {
            let mut pids: Vec<u32> =
                std::fs::read_to_string(cgroups.join(&a.group).join("cgroup.procs"))
                    .unwrap_or_default()
                    .split_whitespace()
                    .filter_map(|s| s.parse().ok())
                    .filter(|pid| procs.iter().any(|p| p.pid == *pid))
                    .collect();
            pids.sort_unstable();
            pids.dedup();
            AdoptionView {
                id: a.id.clone(),
                iface: a.iface.clone(),
                app: a.app.clone(),
                app_name: a.app_name.clone(),
                uid: a.uid,
                running: !pids.is_empty(),
                pids,
            }
        })
        .collect();
    let remembered: BTreeSet<&str> = state
        .subnets
        .keys()
        .chain(state.dns.keys())
        .chain(state.limits.keys())
        .map(String::as_str)
        .chain(bindings.iter().map(|b| b.iface.as_str()))
        .chain(adoptions.iter().map(|a| a.iface.as_str()))
        .filter(|s| *s != "lo")
        .collect();
    let routed = |name: &str| {
        bindings
            .iter()
            .filter(|b| b.iface == name && b.running)
            .count()
            + adoptions
                .iter()
                .filter(|a| a.iface == name && a.running)
                .count()
    };
    let mut interfaces: Vec<Interface> = links
        .iter()
        .filter(|l| {
            l.name != "lo" && (remembered.contains(l.name.as_str()) || (l.up && !l.is_virtual()))
        })
        .map(|l| {
            let bindable = l.bindable();
            Interface {
                name: l.name.clone(),
                kind: l.kind.clone(),
                up: l.up,
                bindable,
                ipv4: l.ipv4_cidrs(),
                gateway: l.gateways.first().cloned(),
                state: if !l.up {
                    "down"
                } else if bindable {
                    "connected"
                } else {
                    "no_route"
                }
                .into(),
                routed: routed(&l.name),
                link_speed: link_speed_bytes(&l.name, &config.sys_class_net),
                dns: state.dns.get(&l.name).cloned().unwrap_or_default(),
                limit: state.limits.get(&l.name).cloned(),
            }
        })
        .collect();
    for name in remembered {
        if !links.iter().any(|l| l.name == name) {
            interfaces.push(Interface {
                name: name.into(),
                kind: classify_kind(name, &config.sys_class_net),
                up: false,
                bindable: false,
                ipv4: vec![],
                gateway: None,
                state: "missing".into(),
                routed: routed(name),
                link_speed: None,
                dns: state.dns.get(name).cloned().unwrap_or_default(),
                limit: state.limits.get(name).cloned(),
            });
        }
    }
    // The monitor's stable ordering: routable first, then glyph kind and name.
    interfaces.sort_by_key(|i| {
        let glyph = if ["tun", "tap", "wg", "ppp", "tailscale", "nordlynx", "proton"]
            .iter()
            .any(|p| i.name.starts_with(p))
        {
            "vpn"
        } else {
            i.kind.as_str()
        };
        let rank = [
            "ethernet",
            "wifi",
            "usb",
            "mobile",
            "bluetooth",
            "vpn",
            "other",
        ]
        .iter()
        .position(|s| *s == glyph)
        .unwrap_or(7);
        (!i.bindable, rank, i.name.clone())
    });
    View {
        interfaces,
        bindings,
        adoptions,
    }
}

pub fn host_veth(iface: &str) -> String {
    // Match Python netns.veth_names: MD5 is only a stable short identifier, never authentication.
    format!(
        "veth0_{:.8}",
        format!("{:x}", md5::compute(iface.as_bytes()))
    )
}

#[derive(Default)]
pub struct Traffic {
    physical: RateTracker,
    namespaces: RateTracker,
}
impl Traffic {
    pub fn sample(&mut self, view: &View, state: &State, base: &Path) -> TrafficSample {
        let physical = read_counters(view.interfaces.iter().map(|i| i.name.as_str()), base)
            .into_iter()
            .map(|(name, c)| (name, iface_sample(c)))
            .collect();
        let names: BTreeSet<&str> = state
            .bindings
            .iter()
            .map(|b| b.iface.as_str())
            .chain(state.adoptions.iter().map(|a| a.iface.as_str()))
            .collect();
        let hosts: BTreeMap<String, String> = names
            .into_iter()
            .map(|n| (host_veth(n), n.into()))
            .collect();
        let namespaces = read_counters(hosts.keys().map(String::as_str), base)
            .into_iter()
            .map(|(host, c)| (hosts[&host].clone(), veth_sample(c)))
            .collect();
        let now = Instant::now();
        TrafficSample {
            t: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs_f64(),
            interfaces: self.physical.update(physical, now),
            namespaces: self.namespaces.update(namespaces, now),
        }
    }
}
