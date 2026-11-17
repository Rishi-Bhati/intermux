//! Tolerant Python-compatible state parsing. Readers never quarantine or write state.
use crate::interface::{integer, text};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{collections::BTreeMap, io, net::Ipv4Addr, path::Path};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Binding {
    pub iface: String,
    pub app: String,
    pub app_name: String,
    pub namespace: String,
    pub subnet: String,
    pub table: i64,
    pub priority: i64,
    pub profile_mode: String,
    pub log_path: String,
    pub created_at: String,
    pub pid: Option<i64>,
    pub uid: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Adoption {
    pub id: String,
    pub iface: String,
    pub app: String,
    pub app_name: String,
    pub slot: i64,
    pub group: String,
    pub mark: i64,
    pub table: i64,
    pub priority: i64,
    pub uid: i64,
    pub pids: Vec<i64>,
    pub created_at: String,
    pub origins: BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Limit {
    pub down: Option<f64>,
    pub up: Option<f64>,
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct State {
    pub version: i64,
    pub subnets: BTreeMap<String, String>,
    pub bindings: Vec<Binding>,
    pub ip_forward_prev: Option<String>,
    pub adoptions: Vec<Adoption>,
    pub dns: BTreeMap<String, Vec<String>>,
    pub limits: BTreeMap<String, Limit>,
}
impl Default for State {
    fn default() -> Self {
        Self {
            version: 1,
            subnets: BTreeMap::new(),
            bindings: vec![],
            ip_forward_prev: None,
            adoptions: vec![],
            dns: BTreeMap::new(),
            limits: BTreeMap::new(),
        }
    }
}

pub fn valid_iface(name: &str) -> bool {
    (1..=15).contains(&name.len())
        && !matches!(name, "." | "..")
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_.:@-".contains(&b))
}
pub fn valid_namespace(name: &str) -> bool {
    name.strip_prefix("ns_")
        .is_some_and(|s| s.len() == 8 && hex(s))
}
fn hex(s: &str) -> bool {
    s.bytes()
        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
pub fn valid_pool_subnet(s: &str) -> bool {
    s.strip_suffix("/24")
        .and_then(|s| s.parse::<Ipv4Addr>().ok())
        .is_some_and(|ip| {
            let [a, b, c, d] = ip.octets();
            a == 10 && b == 200 && (1..=254).contains(&c) && d == 0
        })
}
fn number(v: &Value) -> Option<i64> {
    v.as_bool().map(i64::from).or_else(|| integer(v))
}
fn py_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Null => "None".into(),
        _ => v.to_string(),
    }
}
fn string_default(v: &Value, default: &str) -> String {
    if v.is_null() || v == "" || v == false || v == 0 {
        default.into()
    } else {
        py_string(v)
    }
}
fn required(v: &Value, keys: &[&str]) -> bool {
    v.is_object() && keys.iter().all(|k| !v[k].is_null() && v[k] != "")
}
fn binding(v: &Value) -> Option<Binding> {
    if !required(
        v,
        &["iface", "app", "namespace", "subnet", "table", "priority"],
    ) {
        return None;
    }
    let iface = py_string(&v["iface"]);
    let namespace = py_string(&v["namespace"]);
    let subnet = text(&v["subnet"])?;
    let table = number(&v["table"])?;
    let priority = number(&v["priority"])?;
    if !valid_iface(&iface)
        || !valid_namespace(&namespace)
        || !valid_pool_subnet(subnet)
        || !(1..=0x7fffffff).contains(&table)
        || !(1..=32765).contains(&priority)
    {
        return None;
    }
    let app = py_string(&v["app"]);
    let basename = app.rsplit('/').next().unwrap_or("");
    Some(Binding {
        iface,
        app_name: string_default(&v["app_name"], basename),
        app,
        namespace,
        subnet: subnet.into(),
        table,
        priority,
        profile_mode: string_default(&v["profile_mode"], "default"),
        log_path: string_default(&v["log_path"], ""),
        created_at: string_default(&v["created_at"], ""),
        pid: integer(&v["pid"]).filter(|p| *p > 1),
        uid: v["uid"].as_i64().filter(|u| *u >= 0),
    })
}
fn adoption(v: &Value) -> Option<Adoption> {
    if !required(
        v,
        &[
            "id", "iface", "app", "slot", "group", "mark", "table", "priority", "uid",
        ],
    ) {
        return None;
    }
    let id = py_string(&v["id"]);
    let iface = py_string(&v["iface"]);
    let group = py_string(&v["group"]);
    let slot = number(&v["slot"])?;
    if !(8..=32).contains(&id.len())
        || !hex(&id)
        || !valid_iface(&iface)
        || group != format!("intermux/adopt-{id}")
        || !(1..=200).contains(&slot)
    {
        return None;
    }
    let pids = if v["pids"].is_null() || v["pids"] == false || v["pids"] == 0 || v["pids"] == "" {
        vec![]
    } else {
        v["pids"]
            .as_array()?
            .iter()
            .map(number)
            .collect::<Option<Vec<_>>>()?
    };
    let app = py_string(&v["app"]);
    let basename = app.rsplit('/').next().unwrap_or("");
    Some(Adoption {
        id,
        iface,
        app_name: string_default(&v["app_name"], basename),
        app,
        slot,
        group,
        mark: number(&v["mark"])?,
        table: number(&v["table"])?,
        priority: number(&v["priority"])?,
        uid: integer(&v["uid"])?,
        pids,
        created_at: string_default(&v["created_at"], ""),
        origins: v["origins"]
            .as_object()
            .into_iter()
            .flatten()
            .filter_map(|(pid, path)| {
                let path = path.as_str()?;
                (!pid.is_empty() && pid.chars().all(char::is_numeric))
                    .then(|| (pid.clone(), path.into()))
            })
            .collect(),
    })
}
fn dns_servers(v: &Value) -> Option<Vec<String>> {
    let array = v.as_array()?;
    if !(1..=4).contains(&array.len()) {
        return None;
    }
    let mut servers = vec![];
    for v in array {
        let ip = v.as_str()?.parse::<Ipv4Addr>().ok()?;
        let [a, b, _, _] = ip.octets();
        if ip.is_loopback()
            || ip.is_link_local()
            || ip.is_multicast()
            || ip.is_unspecified()
            || a >= 240
            || (a == 0 && b == 0 && ip == Ipv4Addr::UNSPECIFIED)
        {
            return None;
        }
        let s = ip.to_string();
        if !servers.contains(&s) {
            servers.push(s);
        }
    }
    Some(servers)
}
fn limit(v: &Value) -> Option<Limit> {
    if !v.is_object() {
        return None;
    }
    let rate = |v: &Value| -> Option<Option<f64>> {
        if v.is_null() {
            Some(None)
        } else {
            let n = v.as_f64()?;
            (0.064..=100_000.0).contains(&n).then_some(Some(n))
        }
    };
    let l = Limit {
        down: rate(&v["down"])?,
        up: rate(&v["up"])?,
    };
    (l.down.is_some() || l.up.is_some()).then_some(l)
}

impl State {
    pub fn from_value(v: &Value) -> io::Result<Self> {
        if !v.is_object() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "state must be a JSON object",
            ));
        }
        let parse_list = |key: &str| v[key].as_array().into_iter().flatten();
        let mut bindings = vec![];
        for v in parse_list("bindings") {
            if let Some(b) = binding(v) {
                bindings.push(b);
            } else {
                log::warn!("ignoring malformed binding entry");
            }
        }
        let mut adoptions = vec![];
        for v in parse_list("adoptions") {
            if let Some(a) = adoption(v) {
                adoptions.push(a);
            } else {
                log::warn!("ignoring malformed adoption entry");
            }
        }
        Ok(Self {
            version: number(&v["version"]).unwrap_or(1),
            subnets: v["subnets"]
                .as_object()
                .into_iter()
                .flatten()
                .filter_map(|(iface, cidr)| {
                    let cidr = cidr.as_str()?;
                    (valid_iface(iface) && valid_pool_subnet(cidr))
                        .then(|| (iface.clone(), cidr.into()))
                })
                .collect(),
            bindings,
            adoptions,
            ip_forward_prev: (!v["ip_forward_prev"].is_null())
                .then(|| py_string(&v["ip_forward_prev"])),
            dns: v["dns"]
                .as_object()
                .into_iter()
                .flatten()
                .filter_map(|(k, v)| Some((k.clone(), dns_servers(v)?)))
                .collect(),
            limits: v["limits"]
                .as_object()
                .into_iter()
                .flatten()
                .filter_map(|(k, v)| Some((k.clone(), limit(v)?)))
                .collect(),
        })
    }
}

/// Missing, unreadable and invalid files return errors so a watcher can retain its last good view.
pub fn load_state(path: &Path) -> io::Result<State> {
    let bytes = std::fs::read(path)?;
    let v: Value = serde_json::from_slice(&bytes)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
    State::from_value(&v)
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BindingView {
    pub iface: String,
    pub app: String,
    pub app_name: String,
    pub namespace: String,
    pub pid: Option<i64>,
    pub uid: Option<i64>,
    pub running: bool,
    pub pids: Vec<u32>,
    pub log_path: String,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AdoptionView {
    pub id: String,
    pub iface: String,
    pub app: String,
    pub app_name: String,
    pub uid: i64,
    pub running: bool,
    pub pids: Vec<u32>,
}
