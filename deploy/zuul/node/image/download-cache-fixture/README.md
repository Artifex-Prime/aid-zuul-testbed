# Offline downloader probe

`modules.json` lists Bazel 9.3.2's reachable built-in toolchain dependencies,
excluding entries that only constrain versions (`repo_name = None`). The
probe generates empty local modules for them in its own temporary workspace;
it uses no compiler or toolchain. The empty file registry makes unexpected new
dependencies fail offline. Update this fixture with the Bazel pin.

The payload repository still uses real Bazel `ctx.download` and the node's real
cache setup. `fetch --repo=@payload` avoids unrelated target/toolchain analysis.
These stubs apply only to this isolated probe, never ordinary job or release builds.

The public loopback TLS fixture under `tls/` lets the probe send a checksum-free
token request (Bazel rejects that over plain HTTP). Its test-only JKS truststore
is selected only by the isolated JVM. The token endpoint returns 429, and each
fresh output must still fetch the pinned blob from the read-only distdir without
blob HTTP, using `OCI_GET_TOKEN_ALLOW_FAIL` as rules_oci does.
