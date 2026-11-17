use crate::config::Config;
use nix::{
    fcntl::{fcntl, FcntlArg, FdFlag},
    unistd::getuid,
};
use std::{
    env, fs, io,
    os::{
        fd::{AsFd, FromRawFd},
        unix::fs::{DirBuilderExt, FileTypeExt, MetadataExt, PermissionsExt},
    },
    path::PathBuf,
    time::Duration,
};
use tokio::net::{UnixListener, UnixStream};

pub struct SocketGuard {
    path: Option<PathBuf>,
    inode: u64,
}
impl Drop for SocketGuard {
    fn drop(&mut self) {
        if let Some(path) = &self.path {
            if fs::symlink_metadata(path)
                .is_ok_and(|m| m.file_type().is_socket() && m.ino() == self.inode)
            {
                if let Err(e) = fs::remove_file(path) {
                    log::warn!("cannot remove socket {}: {e}", path.display());
                }
            }
        }
    }
}

pub async fn listen(config: &Config) -> io::Result<(UnixListener, SocketGuard)> {
    if env::var("LISTEN_FDS").as_deref() == Ok("1")
        && env::var("LISTEN_PID")
            .ok()
            .and_then(|p| p.parse::<u32>().ok())
            == Some(std::process::id())
    {
        if !fs::metadata("/proc/self/fd/3")?.file_type().is_socket() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "activation fd 3 is not a socket",
            ));
        }
        // SAFETY: systemd's activation contract transfers ownership of fd 3 to this process.
        // FromRawFd is used exactly once; the listener then owns and closes the descriptor.
        let listener = unsafe { std::os::unix::net::UnixListener::from_raw_fd(3) };
        listener.local_addr()?;
        listener.set_nonblocking(true)?;
        fcntl(listener.as_fd(), FcntlArg::F_SETFD(FdFlag::FD_CLOEXEC)).map_err(io::Error::from)?;
        return Ok((
            UnixListener::from_std(listener)?,
            SocketGuard {
                path: None,
                inode: 0,
            },
        ));
    }
    if let Some(dir) = &config.runtime_directory {
        match fs::DirBuilder::new().mode(0o700).create(dir) {
            Ok(()) => (),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => (),
            Err(e) => return Err(e),
        }
        let meta = fs::symlink_metadata(dir)?;
        if !meta.is_dir() || meta.uid() != getuid().as_raw() {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "runtime directory must be owned by this user and cannot be a symlink",
            ));
        }
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700))?;
    }
    match fs::symlink_metadata(&config.socket) {
        Ok(meta) => {
            if !meta.file_type().is_socket() {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "socket path is not a socket",
                ));
            }
            match tokio::time::timeout(
                Duration::from_millis(500),
                UnixStream::connect(&config.socket),
            )
            .await
            {
                Ok(Err(e))
                    if matches!(
                        e.kind(),
                        io::ErrorKind::ConnectionRefused | io::ErrorKind::NotFound
                    ) =>
                {
                    fs::remove_file(&config.socket)?
                }
                _ => {
                    return Err(io::Error::new(
                        io::ErrorKind::AddrInUse,
                        "a daemon answers at the socket, or it cannot safely be probed",
                    ))
                }
            }
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => (),
        Err(e) => return Err(e),
    }
    let listener = UnixListener::bind(&config.socket)?;
    let guard = SocketGuard {
        path: Some(config.socket.clone()),
        inode: fs::symlink_metadata(&config.socket)?.ino(),
    };
    fs::set_permissions(&config.socket, fs::Permissions::from_mode(0o600))?;
    Ok((listener, guard))
}

pub fn same_user(stream: &UnixStream) -> io::Result<bool> {
    // Tokio's Linux peer_cred uses SO_PEERCRED, and nix wraps getuid safely.
    Ok(stream.peer_cred()?.uid() == getuid().as_raw())
}
