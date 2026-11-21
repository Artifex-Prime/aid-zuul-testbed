{apiVersion: "v1", kind: "List", items: [
  {apiVersion: "v1", kind: "Secret", metadata: {name: "node-ssh", namespace: "zuul-nodes"},
   data: {authorized_keys: ($public | @base64), ssh_host_ed25519_key: ($host | @base64), "ssh_host_ed25519_key.pub": ($hostpub | @base64)}},
  {apiVersion: "v1", kind: "Secret", metadata: {name: "node-ssh", namespace: "zuul-executor"},
   data: {id_rsa: ($login | @base64)}}
]}
