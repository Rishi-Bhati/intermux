use inotify::{EventMask, Inotify, WatchMask};
use netlink_packet_core::NetlinkMessage;
use netlink_packet_route::RouteNetlinkMessage;
use netlink_sys::{protocols::NETLINK_ROUTE, Socket, SocketAddr};
use std::{io, path::Path};
use tokio::{io::unix::AsyncFd, sync::mpsc};

#[derive(Debug, Clone, Copy)]
pub enum Change {
    Interfaces,
    State,
    NetlinkFailed,
    StateWatchFailed,
}

pub fn netlink(tx: mpsc::Sender<Change>) -> io::Result<()> {
    let mut socket = Socket::new(NETLINK_ROUTE)?;
    // Linux multicast masks: RTMGRP_LINK | RTMGRP_IPV4_IFADDR | RTMGRP_IPV4_ROUTE.
    socket.bind(&SocketAddr::new(0, 1 | 0x10 | 0x40))?;
    socket.set_non_blocking(true)?;
    // SAFETY: Socket owns this open fd. No clone or replacement escapes the AsyncFd,
    // so the same descriptor remains valid until the registration is dropped.
    let fd = unsafe { AsyncFd::register(socket) }.map_err(io::Error::from)?;
    tokio::spawn(async move {
        if let Err(e) = netlink_loop(fd, &tx).await {
            log::warn!("netlink notifications unavailable: {e}; rescanning every 5 s");
            let _ = tx.send(Change::NetlinkFailed).await;
        }
    });
    Ok(())
}
async fn netlink_loop(fd: AsyncFd<Socket>, tx: &mpsc::Sender<Change>) -> io::Result<()> {
    let mut buffer = [0u8; 65536];
    loop {
        let mut ready = fd.readable().await?;
        let length = match ready.try_io(|fd| fd.get_ref().recv(&mut &mut buffer[..], 0)) {
            Ok(Ok(n)) => n,
            Ok(Err(e)) if e.raw_os_error() == Some(nix::errno::Errno::ENOBUFS as i32) => {
                // A lost notification requires a full reread, too.
                if tx.send(Change::Interfaces).await.is_err() {
                    return Ok(());
                }
                continue;
            }
            Ok(Err(e)) => return Err(e),
            Err(_) => continue,
        };
        if length == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "netlink closed",
            ));
        }
        // Decode only to recognize notifications, never to build the interface view.
        // A malformed or truncated datagram still triggers a complete ip -j reread.
        let _notification = NetlinkMessage::<RouteNetlinkMessage>::deserialize(&buffer[..length]);
        if tx.send(Change::Interfaces).await.is_err() {
            return Ok(());
        }
    }
}

pub fn state(path: &Path, tx: mpsc::Sender<Change>) -> io::Result<()> {
    let inotify = Inotify::init()?;
    inotify.watches().add(
        path.parent().unwrap_or(Path::new(".")),
        WatchMask::CREATE
            | WatchMask::MOVED_TO
            | WatchMask::CLOSE_WRITE
            | WatchMask::DELETE
            | WatchMask::MOVE_SELF
            | WatchMask::DELETE_SELF
            | WatchMask::ATTRIB,
    )?;
    // SAFETY: Inotify owns the fd, and none of its clones or fd-replacing APIs are
    // used while registered. The descriptor remains open for the AsyncFd lifetime.
    let fd = unsafe { AsyncFd::register(inotify) }.map_err(io::Error::from)?;
    let name = path.file_name().unwrap_or_default().to_owned();
    tokio::spawn(async move {
        if let Err(e) = state_loop(fd, name, &tx).await {
            log::warn!("state notifications unavailable: {e}; polling every 2 s");
            let _ = tx.send(Change::StateWatchFailed).await;
        }
    });
    Ok(())
}
async fn state_loop(
    mut fd: AsyncFd<Inotify>,
    name: std::ffi::OsString,
    tx: &mpsc::Sender<Change>,
) -> io::Result<()> {
    let mut buffer = [0u8; 8192];
    loop {
        let mut ready = fd.readable_mut().await?;
        let (changed, lost) = match ready.try_io(|fd| {
            let events = fd.get_mut().read_events(&mut buffer)?;
            let mut changed = false;
            let mut lost = false;
            for event in events {
                changed |= event.name == Some(name.as_os_str())
                    || event.mask.contains(EventMask::Q_OVERFLOW);
                lost |= event
                    .mask
                    .intersects(EventMask::IGNORED | EventMask::DELETE_SELF | EventMask::MOVE_SELF);
            }
            Ok((changed, lost))
        }) {
            Ok(result) => result?,
            Err(_) => continue,
        };
        if changed && tx.send(Change::State).await.is_err() {
            return Ok(());
        }
        if lost {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "state directory watch lost",
            ));
        }
    }
}
