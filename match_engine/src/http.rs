//! The control port (D23): the D channel at `/d`, plus `/healthz` and
//! `/metrics`, all behind an IP allow-list. The allow-list limits the source
//! only; it is not authentication.

use std::net::{IpAddr, SocketAddr};
use std::sync::Arc;

use axum::extract::{ConnectInfo, Request, State};
use axum::http::StatusCode;
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::Router;

use crate::conn;
use crate::runtime::AppState;

pub fn ctrl_router(app: Arc<AppState>) -> Router {
    Router::new()
        .route("/d", get(conn::d_handler))
        .route("/healthz", get(healthz))
        .route("/metrics", get(metrics))
        .with_state(app.clone())
        .layer(middleware::from_fn_with_state(app, allow_list))
}

/// Refuses connections from outside `CTRL_ALLOWED_CIDRS`.
async fn allow_list(
    State(app): State<Arc<AppState>>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    request: Request,
    next: Next,
) -> Response {
    // A dual-stack listener reports IPv4 peers as IPv4-mapped IPv6 addresses.
    let ip = match peer.ip() {
        IpAddr::V6(v6) => v6
            .to_ipv4_mapped()
            .map(IpAddr::V4)
            .unwrap_or(IpAddr::V6(v6)),
        v4 => v4,
    };
    if app.config.allowed_cidrs.iter().any(|net| net.contains(&ip)) {
        next.run(request).await
    } else {
        StatusCode::FORBIDDEN.into_response()
    }
}

/// 200 once recovery has finished and the journal is writing; 503 while
/// recovering, once a stop has begun, or after a write failure (D10, D23).
async fn healthz(State(app): State<Arc<AppState>>) -> (StatusCode, &'static str) {
    if app.is_healthy() {
        (StatusCode::OK, "ok")
    } else {
        (StatusCode::SERVICE_UNAVAILABLE, "not ready")
    }
}

async fn metrics(State(app): State<Arc<AppState>>) -> impl IntoResponse {
    app.metrics.outbound_backlog.set(app.hub.backlog() as i64);
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "text/plain; version=0.0.4; charset=utf-8",
        )],
        app.metrics.render(),
    )
}
