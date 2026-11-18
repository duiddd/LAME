# Service B: the matching engine (D23).
#
# Build:  docker build -t lame-service-b match_engine
# The expiry threshold is fixed at build time (D15):
#         docker build --build-arg MAX_COMMAND_AGE=1000 -t lame-service-b match_engine

# ---- build ----------------------------------------------------------------
# The image tag pins the same toolchain as rust-toolchain.toml. That file is
# not copied in, so rustup does not try to add rustfmt and clippy here.
FROM rust:1.90.0-slim-bookworm AS build
WORKDIR /src

# Dependencies first, in their own layer, so a change to the sources does not
# rebuild them.
COPY Cargo.toml Cargo.lock ./
RUN mkdir src \
    && echo 'fn main() {}' > src/main.rs \
    && touch src/lib.rs \
    && cargo build --release --locked \
    && rm -rf src

COPY src ./src
ARG MAX_COMMAND_AGE=1000
ENV MAX_COMMAND_AGE=${MAX_COMMAND_AGE}
# touch: make cargo rebuild the crate itself, not reuse the placeholder.
RUN touch src/main.rs src/lib.rs \
    && cargo build --release --locked \
    && mkdir /data

# ---- run ------------------------------------------------------------------
# distroless: no shell, no package manager; the nonroot tag runs as uid 65532.
FROM gcr.io/distroless/cc-debian12:nonroot
COPY --from=build /src/target/release/match_engine /usr/local/bin/match_engine
# Owned by the runtime user, so a named volume mounted here for the first time
# takes the same owner. It is the only place B writes (read-only root).
COPY --from=build --chown=65532:65532 /data /data
VOLUME /data

# The pair config file is mounted at PAIRS_FILE; the image carries none.
ENV EVENT_DB_DIR=/data \
    PAIRS_FILE=/etc/lame/pairs.json \
    MD_ADDR=0.0.0.0:9999 \
    CTRL_ADDR=0.0.0.0:11659 \
    RUST_LOG=info
EXPOSE 9999 11659

HEALTHCHECK --interval=2s --timeout=2s --start-period=60s --retries=3 \
    CMD ["/usr/local/bin/match_engine", "--healthcheck"]
ENTRYPOINT ["/usr/local/bin/match_engine"]
