#!/usr/bin/env bash
# Automated VM Deployment Verification Harness for Ubuntu 22.04 LTS
# Validates Kernel/cgroup pinning, SQLite WAL, and Network singletons.

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m'

echo "=========================================================="
echo "    Schwab Engine - VM Deployment Verification Harness    "
echo "=========================================================="
echo ""

EXIT_CODE=0

fail() {
    echo -e "[ ${RED}FAIL${NC} ] $1"
    EXIT_CODE=1
}

pass() {
    echo -e "[ ${GREEN}PASS${NC} ] $1"
}

info() {
    echo -e "[ ${YELLOW}INFO${NC} ] $1"
}

# 1. Kernel / Cgroups
info "Checking CPU Cores (nproc >= 2 required)"
CORES=$(nproc 2>/dev/null || echo "1")
if [ "$CORES" -ge 2 ]; then
    pass "nproc = $CORES"
else
    fail "Insufficient CPU cores ($CORES). Minimum 2 required for decoupled architecture."
fi

info "Checking Systemd Directives for schwab-trader.service"
# We check using systemctl show. If not running systemd, gracefully warn.
if command -v systemctl >/dev/null 2>&1; then
    TRADER_AFFINITY=$(systemctl show schwab-trader.service --property=CPUAffinity 2>/dev/null || echo "")
    if echo "$TRADER_AFFINITY" | grep -q "0"; then
        pass "schwab-trader.service CPUAffinity=0"
    else
        fail "schwab-trader.service CPUAffinity not pinned to 0"
    fi

    TRADER_NICE=$(systemctl show schwab-trader.service --property=Nice 2>/dev/null || echo "")
    if echo "$TRADER_NICE" | grep -q -- "-10"; then
        pass "schwab-trader.service Nice=-10"
    else
        fail "schwab-trader.service Nice priority is not -10"
    fi

    info "Checking Systemd Directives for schwab-web.service"
    WEB_AFFINITY=$(systemctl show schwab-web.service --property=CPUAffinity 2>/dev/null || echo "")
    if echo "$WEB_AFFINITY" | grep -q "1"; then
        pass "schwab-web.service CPUAffinity=1"
    else
        fail "schwab-web.service CPUAffinity not pinned to 1"
    fi
else
    info "systemctl not found. Skipping systemd directive checks."
fi

# 2. SQLite Health
info "Checking SQLite databases WAL mode"
STATE_DIR="${STATE_DIR:-$(pwd)/state}"

for DB_NAME in "trades.db" "market_baselines.db" "telemetry.db"; do
    DB_PATH="$STATE_DIR/$DB_NAME"
    if [ ! -f "$DB_PATH" ]; then
        info "Database $DB_NAME not found at $DB_PATH (Skipping)"
        continue
    fi
    
    if command -v sqlite3 >/dev/null 2>&1; then
        JMODE=$(sqlite3 "$DB_PATH" "PRAGMA journal_mode;" 2>/dev/null || echo "unknown")
        if [ "$JMODE" == "wal" ]; then
            pass "$DB_NAME journal_mode=wal"
        else
            fail "$DB_NAME journal_mode is '$JMODE', expected 'wal'"
        fi
        
        # Check permissions
        if [ -w "$DB_PATH" ] && [ -r "$DB_PATH" ]; then
            pass "$DB_NAME has correct rw permissions"
        else
            fail "$DB_NAME does not have read-write permissions for current user"
        fi
    else
        info "sqlite3 CLI not found. Skipping WAL check."
    fi
done

# 3. Network / Security
info "Checking FastAPI Web Dashboard on Port 8080"
if command -v curl >/dev/null 2>&1; then
    if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/health 2>/dev/null | grep -q "200"; then
        pass "GET /health returned 200 OK"
    else
        info "Web server not responding on http://127.0.0.1:8080/health (May be offline)"
    fi
else
    info "curl not found. Skipping network check."
fi

info "Checking Schwab Streaming WebSocket Singleton constraint"
if command -v netstat >/dev/null 2>&1; then
    ESTABLISHED_STREAMS=$(netstat -anp 2>/dev/null | grep -i ":443" | grep "ESTABLISHED" | wc -l || echo "0")
    if [ "$ESTABLISHED_STREAMS" -le 1 ]; then
        pass "WebSocket limit respected: $ESTABLISHED_STREAMS external TLS streams found."
    else
        # We don't fail immediately because other TLS connections might exist, but we flag it.
        info "$ESTABLISHED_STREAMS external TLS connections active. Verify that only 1 is Schwab Streamer."
    fi
else
    info "netstat not found. Skipping singleton stream check."
fi

echo "=========================================================="
if [ "$EXIT_CODE" -eq 0 ]; then
    echo -e "${GREEN}ALL APPLICABLE PRE-FLIGHT CHECKS PASSED.${NC}"
else
    echo -e "${RED}ONE OR MORE CHECKS FAILED.${NC}"
fi
exit $EXIT_CODE
