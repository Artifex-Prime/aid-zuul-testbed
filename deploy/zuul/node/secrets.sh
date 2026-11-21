#!/usr/bin/env bash
# Once: make the node's host key and the executor's login key. Save the JSON in the keychain;
# reapply that same JSON on rotation/redeploy. No farm key: a projected token identifies the PR client.
set -euo pipefail
for tool in ssh-keygen jq; do
  command -v "$tool" > /dev/null || { echo "node/secrets.sh needs $tool on PATH (ssh-keygen is OpenSSH's)" >&2; exit 1; }
done
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
umask 077
ssh-keygen -q -t ed25519 -N '' -C zuul-executor -f "$work/login"
ssh-keygen -q -t ed25519 -N '' -C zuul-node -f "$work/host"
jq -n --rawfile login "$work/login" --rawfile public "$work/login.pub" --rawfile host "$work/host" --rawfile hostpub "$work/host.pub" \
  -f "$here/secrets.jq"
