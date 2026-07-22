#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$root/scripts/prepare-managed-runtime-dev.sh" sadtalker "$@"
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
exec bash scripts/prepare-sadtalker-dev.sh "$@"
