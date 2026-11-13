# Caddy of the observability stack + CrowdSec bouncer (HTTP handler).
# The bouncer module tag must require the same Caddy version as CADDY_VERSION in its go.mod
# (v0.13.0 -> caddy v2.10.2; v0.14+ pulls caddy v2.11.x, i.e. would silently upgrade Caddy).
ARG CADDY_VERSION=2.10.2
ARG CADDY_BUILDER_IMAGE=caddy:2.10.2-builder-alpine@sha256:4cff3ae272ea05842adb4546e35961e4947aeea65f80fa1eb62a35b960e84971
ARG CADDY_BASE_IMAGE=caddy:2.10.2-alpine@sha256:4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d

FROM ${CADDY_BUILDER_IMAGE} AS builder
ARG CADDY_VERSION
ARG CROWDSEC_BOUNCER_VERSION=v0.13.0
# Go module sources. proxy.golang.org redirects zip downloads to storage.googleapis.com, which
# hangs or answers 403 "not available in your location" in some regions (google.golang.org vanity
# imports too). goproxy.cn goes first (zip 200 from the host and from the builder; goproxy.io
# answers 403 for zips). "|" moves on to the next source on ERROR only, then direct VCS.
# Checksums stay verified: GOSUMDB=sum.golang.org (the go command fetches it through the
# proxy's /sumdb/ endpoint when supported). Never set GOSUMDB=off / GONOSUMDB here.
ARG GOPROXY=https://goproxy.cn|https://proxy.golang.org|direct
ARG GOSUMDB=sum.golang.org
ENV GOPROXY=${GOPROXY} GOSUMDB=${GOSUMDB}
RUN xcaddy build "v${CADDY_VERSION}" \
        --with "github.com/hslatman/caddy-crowdsec-bouncer/http@${CROWDSEC_BOUNCER_VERSION}" \
    && /usr/bin/caddy list-modules | grep -q '^http.handlers.crowdsec$'

FROM ${CADDY_BASE_IMAGE}
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
