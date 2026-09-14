FROM golang:1.26.1-bookworm@sha256:ab3d6955bbc813a0f3fdf220c1d817dd89c0b3f283777db8ece4a32fe7858edd AS builder
ARG MINIO_COMMIT=7aac2a2c5b7c882e68c1ce017d8256be2feea27f
ARG MC_COMMIT=77f82e18b5401a65958f1619df6ebb994634bd88
RUN git clone https://github.com/minio/minio.git /src/minio && cd /src/minio && git checkout "$MINIO_COMMIT" && CGO_ENABLED=0 go build -trimpath -o /out/minio .
RUN git clone https://github.com/minio/mc.git /src/mc && cd /src/mc && git checkout "$MC_COMMIT" && CGO_ENABLED=0 go build -trimpath -o /out/mc .
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY --from=builder /out/ /usr/local/bin/
ENTRYPOINT ["minio"]
