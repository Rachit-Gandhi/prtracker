FROM golang:1.24-alpine

WORKDIR /app

# Install git (needed for go get)
RUN apk add --no-cache git

# Copy go mod and sum files
COPY go.mod go.sum ./

# Download all dependencies
RUN go mod download

# Copy the source code
COPY *.go ./

# Build the application
RUN go build -o /github-pr-tracker

# Set the entry point
ENTRYPOINT ["/github-pr-tracker"]