#!/usr/bin/env bash
# Once: make the post node's host key, and let in the executor's one login key. Zuul's executor logs in to every node
# with the one key its zuul.conf names (/root/.ssh/id_rsa, the PR node's secret in zuul-executor), so the post node
# authorizes that key's public half, read from the PR node's secret JSON on standard input:
#
#   aid-keychain get zuul-node-ssh | deploy/zuul/post-node/secrets.sh | aid-keychain set zuul-post-node-ssh
#
# It prints one Secret, in zuul-post, with no private key of the executor's. Save it in the keychain and reapply that
# same JSON on a redeploy; the host key's public half goes into aid-zuul-config's providers.yaml.
set -euo pipefail
for tool in ssh-keygen jq; do
  command -v "$tool" > /dev/null || { echo "post-node/secrets.sh needs $tool on PATH (ssh-keygen is OpenSSH's)" >&2; exit 1; }
done
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
umask 077
jq -ej '.items[] | select(.metadata.namespace == "zuul-nodes" and .metadata.name == "node-ssh") | .data.authorized_keys | @base64d' \
  > "$work/login.pub" || { echo "post-node/secrets.sh reads the PR node's secret JSON (zuul-node-ssh) on standard input" >&2; exit 1; }
ssh-keygen -l -f "$work/login.pub" > /dev/null || { echo "post-node/secrets.sh: no public key in the PR node's authorized_keys" >&2; exit 1; }
ssh-keygen -q -t ed25519 -N '' -C zuul-post-node -f "$work/host"
jq -n --rawfile public "$work/login.pub" --rawfile host "$work/host" --rawfile hostpub "$work/host.pub" -f "$here/secrets.jq"
