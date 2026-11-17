#!/bin/bash
set -eu
TASK_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
export PATH="/Users/weiwang/.nvm/versions/node/v24.8.0/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"
cd "$TASK_ROOT"
exec "$TASK_ROOT/.venv/bin/python" -m daily_review.cli "$@"
