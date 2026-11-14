# urgentry v0.2.11 (Functional Source License 1.1, Apache 2.0 future license): Sentry-compatible
# error tracker of the observability stack. Same layout as the upstream Dockerfile
# (https://github.com/urgentry/urgentry, tag v0.2.11), but base images are pinned by digest and
# the cloned tag must resolve to the expected commit.
ARG URGENTRY_BUILDER_IMAGE=golang:1.26-alpine@sha256:30b1e546a9c7fa3b747c2bdba3a0c7bdf3a02ebb4c5b8405d2f948d7e8aac4ec
ARG URGENTRY_BASE_IMAGE=alpine:3.21@sha256:bc21487101ae47172509130ee1092cc273a831968d64b617776d1f69c81e42ab

FROM ${URGENTRY_BUILDER_IMAGE} AS builder
ARG URGENTRY_VERSION=v0.2.11
ARG URGENTRY_COMMIT=9df72c5115458a00b3fba2cac189d2f458d24470
ARG URGENTRY_BUILD_TAGS=netgo,osusergo
# Go module sources. proxy.golang.org redirects zip downloads to storage.googleapis.com, which
# hangs or answers 403 "not available in your location" in some regions. goproxy.cn goes first,
# "," moves on to the next source on 404/410 only, then direct VCS (same mirrors as
# ops/observability/caddy/Dockerfile). Checksums stay verified: GOSUMDB=sum.golang.org.
# Never set GOSUMDB=off / GONOSUMDB here.
ARG GOPROXY=https://goproxy.cn,https://proxy.golang.org,direct
ARG GOSUMDB=sum.golang.org
ENV GOPROXY=${GOPROXY} GOSUMDB=${GOSUMDB}
RUN apk add --no-cache git
WORKDIR /build
RUN git clone --depth 1 --branch "${URGENTRY_VERSION}" https://github.com/urgentry/urgentry.git . \
    && test "$(git rev-parse HEAD)" = "${URGENTRY_COMMIT}" \
    || { echo "urgentry ${URGENTRY_VERSION} is not commit ${URGENTRY_COMMIT}" >&2; exit 1; }
RUN go mod download
RUN VERSION="${URGENTRY_VERSION}" URGENTRY_BUILD_TAGS="${URGENTRY_BUILD_TAGS}" \
    sh ./scripts/build-urgentry.sh --output /build/urgentry

FROM ${URGENTRY_BASE_IMAGE}
ARG URGENTRY_VERSION=v0.2.11
ARG URGENTRY_COMMIT=9df72c5115458a00b3fba2cac189d2f458d24470
LABEL org.opencontainers.image.title="Urgentry" \
      org.opencontainers.image.version="${URGENTRY_VERSION}" \
      org.opencontainers.image.revision="${URGENTRY_COMMIT}" \
      org.opencontainers.image.source="https://github.com/urgentry/urgentry" \
      org.opencontainers.image.licenses="FSL-1.1-Apache-2.0"
# uid/gid 10001: the bind-mounted /data must belong to 10001:10001 (make init-dirs).
RUN apk add --no-cache ca-certificates tzdata \
    && addgroup -S -g 10001 urgentry \
    && adduser -S -D -H -G urgentry -u 10001 urgentry \
    && mkdir -p /data \
    && chown -R 10001:10001 /data
COPY --from=builder /build/urgentry /usr/local/bin/urgentry
ENV URGENTRY_DATA_DIR=/data
ENV URGENTRY_HTTP_ADDR=:8080
EXPOSE 8080
VOLUME /data
USER 10001:10001
ENTRYPOINT ["urgentry"]
CMD ["serve", "--role=all"]
