#!/bin/bash
# run-forge.sh — Wrapper to run Forge Driver with network check
set -euo pipefail

export PATH="/opt/miniconda3/envs/core/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/tai/.local/bin:$PATH"
export HOME="/Users/tai"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/logs/forge.log"
mkdir -p "$SCRIPT_DIR/logs"

# Check network connectivity (api.notion.com and api.linear.app)
check_network() {
    python3 -c "
import socket, sys
for host in ['api.linear.app', 'api.notion.com']:
    try:
        socket.create_connection((host, 443), timeout=5)
        sys.exit(0)
    except Exception:
        pass
sys.exit(1)
"
}

if ! check_network; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARNING] No internet connection. Skipping forge run safely." >> "$LOG_FILE"
    exit 0
fi

# Run python forge driver and tee output to log
/opt/miniconda3/envs/core/bin/python3 "$SCRIPT_DIR/forge_driver.py" "$@" 2>&1 | tee -a "$LOG_FILE"
