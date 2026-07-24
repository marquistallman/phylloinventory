#  Multi-stage build para kalman-worker (Go)
FROM golang:1.25-alpine AS builder
WORKDIR /src
RUN apk add --no-cache git
COPY go.mod ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /out/kalman-worker .

FROM alpine:3.19
RUN apk add --no-cache ca-certificates tzdata
WORKDIR /app
COPY --from=builder /out/kalman-worker /app/kalman-worker
EXPOSE 8300
ENTRYPOINT ["/app/kalman-worker"]
