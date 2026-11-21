#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
# binutils: strip/readelf for the image policy checks and post release folder (#1350).
# gcc/libc6-dev: the cheap layer's root guard tests compile their private C fixtures (#1520).
apt-get install -y --no-install-recommends binutils ca-certificates curl gcc libc6-dev git jq openssh-server openssl python3 rsync xz-utils
rm -rf /var/lib/apt/lists/*
rm -f /etc/ssh/ssh_host_*
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
download() {
  curl --fail --silent --show-error --location --retry 3 "$1" -o "$work/download"
  echo "$2  $work/download" | sha256sum -c -
}
download "https://github.com/bazelbuild/bazelisk/releases/download/v${BAZELISK_VERSION}/bazelisk-linux-amd64" "$BAZELISK_SHA256"
install -m 755 "$work/download" /usr/local/bin/bazelisk
download "https://releases.bazel.build/${BAZEL_VERSION}/release/bazel-${BAZEL_VERSION}-linux-x86_64" "$BAZEL_SHA256"
install -m 755 "$work/download" "/usr/local/bin/bazel-${BAZEL_VERSION}"
while read -r tool _rule version sum; do
  case "$tool" in
    node)
      download "https://nodejs.org/dist/v$version/node-v$version-linux-x64.tar.xz" "$sum"
      mkdir -p /opt/node
      tar -xJf "$work/download" --strip-components=1 -C /opt/node ;;
    caddy)
      download "https://github.com/caddyserver/caddy/releases/download/v$version/caddy_${version}_linux_amd64.tar.gz" "$sum"
      tar -xzf "$work/download" -C /usr/local/bin caddy ;;
    kubectl)
      download "https://dl.k8s.io/release/v$version/bin/linux/amd64/kubectl" "$sum"
      install -m 755 "$work/download" /usr/local/bin/kubectl ;;
  esac
done < /tmp/tools.pin
# The Swift runtime alone (#1521): the shared libraries of swift.org's release that .swift-version pins, from
# usr/lib/swift/linux, where the farm's installed toolchain (build/farm/toolchain/) links `aid` to find them (its
# RUNPATH), so that the ingress suite runs a farm-built `aid` here. Nothing from bin/: still no compiler on the node.
download "https://download.swift.org/swift-${SWIFT_VERSION}-release/ubuntu2604/swift-${SWIFT_VERSION}-RELEASE/swift-${SWIFT_VERSION}-RELEASE-ubuntu26.04.tar.gz" "$SWIFT_SHA256"
mkdir -p /usr/lib/swift/linux
tar -xzf "$work/download" -C /usr/lib/swift/linux --strip-components=5 --no-same-owner \
  --wildcards --no-wildcards-match-slash '*/usr/lib/swift/linux/*.so*'
rm "$work/download"
groupadd --gid 10001 zuul
useradd --uid 10001 --gid zuul --create-home --shell /bin/bash zuul
# Only public-key authentication is enabled, but OpenSSH also refuses locked accounts.
passwd -d zuul
mkdir -p /run/sshd
