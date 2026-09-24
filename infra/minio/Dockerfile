FROM golang:1.24.8 AS build

# The upstream community image is no longer publicly pullable. Build the
# patched upstream release directly from its immutable Go module version.
RUN CGO_ENABLED=0 go install github.com/minio/minio@RELEASE.2025-10-15T17-29-55Z

FROM alpine:3.22
RUN apk add --no-cache ca-certificates curl
COPY --from=build /go/bin/minio /usr/local/bin/minio
ENTRYPOINT ["minio"]
