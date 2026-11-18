FROM golang:1.24.8-bookworm@sha256:4ed690d6649d63c312b99a6120025ec79ce3b542968a37da53d6236c7c61a848 AS build

ARG MINIO_COMMIT=9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a
WORKDIR /src
RUN git init . \
    && git remote add origin https://github.com/minio/minio.git \
    && git fetch --depth=1 origin ${MINIO_COMMIT} \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${MINIO_COMMIT}"
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOTOOLCHAIN=local go build -p 2 -trimpath \
    -ldflags="$(MINIO_RELEASE=RELEASE go run buildscripts/gen-ldflags.go 2025-10-15T17:29:55Z)" \
    -o /out/minio .

FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/minio /usr/local/bin/minio
COPY --from=build /src/LICENSE /usr/share/doc/minio/LICENSE
LABEL org.opencontainers.image.source="https://github.com/minio/minio" \
    org.opencontainers.image.revision="9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a" \
    org.opencontainers.image.version="RELEASE.2025-10-15T17-29-55Z" \
    org.opencontainers.image.licenses="AGPL-3.0-only"
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/minio_data", "--console-address", ":9001"]
