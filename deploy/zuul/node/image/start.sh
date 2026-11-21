#!/bin/bash
set -euo pipefail
mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
cp /etc/node-ssh/ssh_host_ed25519_key "$HOME/.ssh/ssh_host_ed25519_key"
chmod 600 "$HOME/.ssh/ssh_host_ed25519_key"
# Shared cache entries are immutable to jobs. Hash directories stay private:
# Bazel writes canonical-ID markers and temporary files even for existing hashes.
# Only individual files are symlinked; atomic cache writes replace a private link.
cache=$HOME/.cache/bazel/repository-cache/content_addressable/sha256
mkdir -p "$cache"
for entry in /var/cache/bazel/repository-cache/content_addressable/sha256/*; do
  [ -d "$entry" ] || continue
  private=$cache/$(basename "$entry")
  mkdir -p "$private"
  for file in "$entry"/*; do
    [ -f "$file" ] || continue
    target=$private/$(basename "$file")
    # Static nodes retain their home across container restarts during migration.
    # Keep private downloads and markers; only seed entries that are absent.
    if [ ! -e "$target" ] && [ ! -L "$target" ]; then ln -s "$file" "$target"; fi
  done
done
exec /usr/sbin/sshd -D -e
