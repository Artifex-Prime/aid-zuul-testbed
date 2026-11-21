{apiVersion: "v1", kind: "Secret", metadata: {name: "node-ssh", namespace: "zuul-gate-compute"},
 data: {authorized_keys: ($public | @base64), ssh_host_ed25519_key: ($host | @base64), "ssh_host_ed25519_key.pub": ($hostpub | @base64)}}
