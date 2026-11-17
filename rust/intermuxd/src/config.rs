use std::{env, io, path::PathBuf};

#[derive(Debug, Clone)]
pub struct Config {
    pub socket: PathBuf,
    pub state: PathBuf,
    pub sys_class_net: PathBuf,
    pub proc: PathBuf,
    pub runtime_directory: Option<PathBuf>,
}

impl Config {
    pub fn parse() -> io::Result<Option<Self>> {
        let mut socket = None;
        let mut state = PathBuf::from("/var/lib/intermux/state.json");
        let mut sys_class_net = PathBuf::from("/sys/class/net");
        let mut proc = PathBuf::from("/proc");
        let mut args = env::args_os().skip(1);
        while let Some(arg) = args.next() {
            if arg == "--version" {
                println!("intermuxd {}", intermux_common::protocol::DAEMON_VERSION);
                return Ok(None);
            }
            let target = match arg.to_str() {
                Some("--socket") => &mut socket,
                Some("--state") => { state = args.next().ok_or_else(|| invalid("--state needs a path"))?.into(); continue; }
                Some("--sys-class-net") => { sys_class_net = args.next().ok_or_else(|| invalid("--sys-class-net needs a path"))?.into(); continue; }
                Some("--proc") => { proc = args.next().ok_or_else(|| invalid("--proc needs a path"))?.into(); continue; }
                _ => return Err(invalid("usage: intermuxd [--socket PATH] [--state PATH] [--sys-class-net PATH] [--proc PATH] [--version]")),
            };
            *target = Some(PathBuf::from(
                args.next()
                    .ok_or_else(|| invalid("--socket needs a path"))?,
            ));
        }
        let runtime = env::var_os("XDG_RUNTIME_DIR")
            .filter(|s| !s.is_empty())
            .ok_or_else(|| invalid("XDG_RUNTIME_DIR is unset"))?;
        let runtime_directory = if socket.is_none() {
            Some(PathBuf::from(runtime).join("intermux"))
        } else {
            None
        };
        let socket =
            socket.unwrap_or_else(|| runtime_directory.as_ref().unwrap().join("daemon.sock"));
        Ok(Some(Self {
            socket,
            state,
            sys_class_net,
            proc,
            runtime_directory,
        }))
    }
}
fn invalid(s: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, s)
}
