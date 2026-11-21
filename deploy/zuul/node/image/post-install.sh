#!/bin/bash
# The post node's additions to the node (#1633): what the Swift toolchain Bazel downloads needs from the host, for
# deploy/release-folder.sh's clean local build (#1298), which the PR node never runs. swift.org's list for its Ubuntu
# 26.04 image (swiftlang/swift-docker, 6.4/ubuntu/26.04), as the farm's workers have it (build/farm/worker/install.sh).
# The runtime check uses a pinned, off-PATH Swift fixture; release builds still use Bazel's own toolchain.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  binutils-gold libc6-dev libcurl4-openssl-dev libedit2 libgcc-15-dev libncurses-dev libpython3-dev libsqlite3-0 \
  libstdc++-15-dev libxml2-dev libz3-dev pkg-config tzdata unzip zlib1g-dev
rm -rf /var/lib/apt/lists/*

# The post runtime check runs under arc-imagecheck's closed egress policy. Keep its pinned
# toolchain fixture off PATH; the clean release invocation still fetches its own toolchain.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
release="swift-${SWIFT_VERSION}-RELEASE"
curl --fail --silent --show-error --location --retry 3 -o "$work/swift.tar.gz" \
  "https://download.swift.org/swift-${SWIFT_VERSION}-release/ubuntu2604/${release}/${release}-ubuntu26.04.tar.gz"
echo "${SWIFT_SHA256}  $work/swift.tar.gz" | sha256sum -c -
mkdir -p /opt/zuul-node-check/swift
tar -xzf "$work/swift.tar.gz" -C /opt/zuul-node-check/swift --strip-components=1 --no-same-owner
