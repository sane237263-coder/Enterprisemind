#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p .local/minio-build
if [ ! -f .local/minio-build/source.tar.gz ]; then
    curl -fL --retry 2 --max-time 120 \
        https://codeload.github.com/minio/minio/tar.gz/9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a \
        -o .local/minio-build/source.tar.gz
fi
printf '%s\n' '45521908307306e925c98d629e1c17d78c8b72b6ee242b1bfb1409f7d8ee5841  .local/minio-build/source.tar.gz' | sha256sum -c -
docker build -f deploy/minio/Dockerfile.test -t enterprise-mind-minio-test:2025-10-15 .local/minio-build
