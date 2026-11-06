//! The `--healthcheck` subcommand: the image has no `curl`, so the binary
//! checks itself (D23). It asks the control port for `/healthz`.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::time::Duration;

/// True if `GET /healthz` on `addr` answers 200.
pub fn check(addr: SocketAddr) -> bool {
    let limit = Duration::from_secs(2);
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, limit) else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(limit));
    let _ = stream.set_write_timeout(Some(limit));
    let request = "GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n";
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = String::new();
    let _ = stream.read_to_string(&mut response);
    response
        .lines()
        .next()
        .is_some_and(|status| status.split_whitespace().nth(1) == Some("200"))
}

/// Where the health check looks: this machine, on the port `CTRL_ADDR` names
/// (default 11659).
pub fn local_target() -> SocketAddr {
    let port = std::env::var("CTRL_ADDR")
        .ok()
        .and_then(|addr| addr.parse::<SocketAddr>().ok())
        .map_or(11659, |addr| addr.port());
    SocketAddr::from(([127, 0, 0, 1], port))
}
