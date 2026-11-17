use intermux_common::protocol::MAX_LINE;
use serde_json::Value;
use std::{io, sync::Arc};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{unix::OwnedReadHalf, UnixStream},
    sync::{mpsc, Notify},
};

pub enum Command {
    Request(u64, Vec<u8>),
    Disconnected(u64),
}
pub struct Client {
    pub hello: bool,
    pub topics: Vec<String>,
    pub out: mpsc::Sender<Vec<u8>>,
    pub cancel: Arc<Notify>,
}
impl Client {
    pub fn send(&self, value: &Value) -> bool {
        let mut bytes = serde_json::to_vec(value).expect("JSON Value serializes");
        bytes.push(b'\n');
        self.out.try_send(bytes).is_ok()
    }
    pub fn subscribed(&self, topic: &str) -> bool {
        self.topics.iter().any(|s| s == topic)
    }
}
pub fn client() -> (Client, mpsc::Receiver<Vec<u8>>) {
    let (out, rx) = mpsc::channel(64);
    (
        Client {
            hello: false,
            topics: vec![],
            out,
            cancel: Arc::new(Notify::new()),
        },
        rx,
    )
}
pub async fn serve(
    id: u64,
    stream: UnixStream,
    mut output: mpsc::Receiver<Vec<u8>>,
    cancel: Arc<Notify>,
    commands: mpsc::Sender<Command>,
) {
    let (read, mut write) = stream.into_split();
    let writer = async {
        while let Some(bytes) = output.recv().await {
            write.write_all(&bytes).await?;
        }
        write.shutdown().await
    };
    tokio::select! {
        _ = read_requests(id, read, &commands) => (),
        _ = writer => (),
        _ = cancel.notified() => (),
    }
    let _ = commands.send(Command::Disconnected(id)).await;
}
async fn read_requests(
    id: u64,
    read: OwnedReadHalf,
    commands: &mpsc::Sender<Command>,
) -> io::Result<()> {
    let mut reader = BufReader::new(read);
    let mut line = Vec::new();
    loop {
        let bytes = reader.fill_buf().await?;
        if bytes.is_empty() {
            return Ok(());
        }
        let end = bytes.iter().position(|b| *b == b'\n');
        let count = end.unwrap_or(bytes.len());
        if line.len() + count > MAX_LINE {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "line exceeds 64 KiB",
            ));
        }
        line.extend_from_slice(&bytes[..count]);
        reader.consume(count + usize::from(end.is_some()));
        if end.is_some()
            && commands
                .send(Command::Request(id, std::mem::take(&mut line)))
                .await
                .is_err()
        {
            return Ok(());
        }
    }
}
