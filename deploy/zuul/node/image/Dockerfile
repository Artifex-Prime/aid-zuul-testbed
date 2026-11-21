# Orchestration node: ordinary Bazel builds execute on the farm; the base node carries only the Swift runtime.
# Build from the repository root. `check` must succeed before publishing `node`, and `post-check` before `post`.
# `post` is the post node's (#1633, deploy/zuul/post-node/): the node with what Bazel's Swift toolchain needs from the
# host, for the release folder's clean local build. The PR node stays `node`, without them.
# Swift's release, as .swift-version and the farm's workers pin it: `node` carries its runtime libraries alone (#1521),
# `post` also carries an off-PATH toolchain fixture for offline acceptance; release builds still use their own toolchain.
ARG SWIFT_VERSION=6.4.0
ARG SWIFT_SHA256=c966d52a195724238fcc082afefac8f274dff1f30cf2ace5f93ec684c17b0026
FROM docker.io/library/ubuntu:26.04@sha256:6a06111c3b0a8600feadb270aec771dc239bf407b25625f93f44ab00e11f4d3c AS node
ARG BAZEL_VERSION=9.3.2
ARG BAZEL_SHA256=2e14c55c298752d71e8a0bbd762665ffce5966c9ad00ec5d1a8b82a58e7bfa06
ARG BAZELISK_VERSION=1.30.0
ARG BAZELISK_SHA256=a248ae2b2b62b5baebb748f6fba7203bff1a27e5d01b55399a8406744cb539e8
ARG SWIFT_VERSION
ARG SWIFT_SHA256
COPY ci/tools.pin /tmp/tools.pin
COPY deploy/zuul/node/image/install.sh /tmp/install.sh
RUN bash /tmp/install.sh && rm /tmp/install.sh /tmp/tools.pin
COPY deploy/zuul/node/image/sshd_config /etc/ssh/sshd_config
COPY deploy/zuul/node/image/bazelrc /etc/bazel.bazelrc
COPY --chmod=755 deploy/zuul/node/image/bazel /usr/local/bin/bazel
COPY deploy/zuul/node/image/start.sh /usr/local/bin/start-node
COPY deploy/zuul/node/image/farm-credential.py /usr/local/bin/zuul-farm-credential
ENV HOME=/home/zuul PATH=/opt/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /home/zuul
USER 10001:10001
LABEL org.opencontainers.image.title="zuul-node" org.opencontainers.image.source="https://github.com/kristofk/AIDepartment"
EXPOSE 2222
ENTRYPOINT ["/usr/local/bin/start-node"]

# Ship the real cache regression and its file-only module fixture for offline image checks.
COPY .bazelversion /opt/zuul-node-check/.bazelversion
COPY deploy/zuul/node/image/start.sh deploy/zuul/node/image/bazelrc deploy/zuul/node/image/test-download-cache.py /opt/zuul-node-check/deploy/zuul/node/image/
COPY deploy/zuul/node/image/download-cache-fixture /opt/zuul-node-check/deploy/zuul/node/image/download-cache-fixture

FROM node AS check
COPY deploy/zuul/node/image/check.sh /tmp/check.sh
RUN USE_BAZEL_VERSION=${BAZEL_VERSION} bash /tmp/check.sh \
    && BAZEL=/usr/local/bin/bazel python3 /opt/zuul-node-check/deploy/zuul/node/image/test-download-cache.py

FROM node AS post
USER root
COPY deploy/zuul/node/image/post-install.sh /tmp/post-install.sh
RUN bash /tmp/post-install.sh && rm /tmp/post-install.sh
USER 10001:10001
LABEL org.opencontainers.image.title="zuul-post-node"

# The node's own check, then the toolchain's: the farm worker's Swift (build/farm/worker/Dockerfile), the release
# .swift-version pins, the very tarball Bazel's hermetic toolchain is. ci/zuul-node.test.mjs holds the two equal.
FROM post AS post-check
ARG SWIFT_VERSION
ARG SWIFT_SHA256
COPY deploy/zuul/node/image/check.sh deploy/zuul/node/image/post-check.sh /tmp/
RUN USE_BAZEL_VERSION=${BAZEL_VERSION} bash /tmp/check.sh && bash /tmp/post-check.sh /opt/zuul-node-check/swift/usr
