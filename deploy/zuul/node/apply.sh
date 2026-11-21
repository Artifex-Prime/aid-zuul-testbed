#!/usr/bin/env bash
# Update only the admin-owned disposable template and its prerequisites, from merged main.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
exec "$here/../apply-disposable.sh" node "$@"
