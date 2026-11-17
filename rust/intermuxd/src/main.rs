mod config;
mod events;
mod server;
mod socket;
mod transport;
mod view;
mod watch;

#[cfg(test)]
mod tests;

#[tokio::main]
async fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let result = match config::Config::parse() {
        Ok(Some(config)) => server::run(config).await,
        Ok(None) => Ok(()),
        Err(e) => Err(e),
    };
    if let Err(e) = result {
        log::error!("{e}");
        std::process::exit(1);
    }
}
