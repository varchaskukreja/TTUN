# Shopping Sessions (Databricks track) submission. The pipeline lives in shopping-sessions/.
FROM rust:1-slim-bookworm AS build
ARG TARGETARCH
WORKDIR /src
COPY shopping-sessions/Cargo.toml shopping-sessions/Cargo.lock ./
COPY shopping-sessions/src ./src
# An explicit --target keeps RUSTFLAGS off build scripts, so they run on any build host.
RUN if [ "$TARGETARCH" = "amd64" ]; then \
      RUSTFLAGS="-C target-cpu=x86-64-v3" cargo build --release --locked --target x86_64-unknown-linux-gnu && \
      cp target/x86_64-unknown-linux-gnu/release/ss /ss; \
    else \
      cargo build --release --locked && cp target/release/ss /ss; \
    fi

FROM debian:bookworm-slim
COPY --from=build /ss /app/ss
ENV GLIBC_TUNABLES=glibc.malloc.hugetlb=1
USER 1000:1000
CMD ["/app/ss"]
