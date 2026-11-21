# Loopback TLS fixture

The isolated download probe generates a fresh RSA key and one-day self-signed
certificate for 127.0.0.1 in its private temporary directory, then removes them
on exit. No key, certificate or truststore is stored in this repository.

Both pinned execution images already supply `/usr/bin/openssl`: the node installs
it explicitly, and the farm worker's `ca-certificates` package depends on it.
`build/images/packages.lock.json` pins that dependency and its checksum. The probe
uses Python's standard library to write a certificate-only JKS v2 truststore
(password: `fixture`); it needs no `keytool`, extra JDK or network access.

Bazel rejects checksum-free plain HTTP. TLS lets each fresh output's uncacheable
token request reach the real loopback HTTP 429 endpoint before a digest-verified
distdir read. Only the isolated probe JVMs select the generated truststore;
ordinary jobs use their normal trust configuration. Generation writes are included
in `//build/images:infrastructure_storage_test`'s existing storage budget.
