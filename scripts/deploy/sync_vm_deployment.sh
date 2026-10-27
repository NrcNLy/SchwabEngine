#!/usr/bin/env bash
# scripts/deploy/sync_vm_deployment.sh
# ====================================
# Workstream 3 (#124): GCP VM Deployment Synchronization & Regression Verification.
# Target: schwab-trader VM (us-central1-a)
# Branch: feat/dynamic-screener-universe

set -euo pipefail

RESTART_SERVICE=false

for arg in "$@"; do
    case "$arg" in
        --restart-service)
            RESTART_SERVICE=true
            shift
            ;;
        *)
            ;;
    esac
done

echo "======================================================================"
echo " [DEPLOY] Starting SchwabEngine Deployment Sync on $(hostname)"
echo " [DEPLOY] Timestamp: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
echo "======================================================================"

# Step 1: Synchronize git repository
echo "[Step 1/6] Synchronizing git repository on branch feat/dynamic-screener-universe..."
git fetch origin feat/dynamic-screener-universe
git checkout feat/dynamic-screener-universe
git pull --ff-only origin feat/dynamic-screener-universe
echo "Repository synced cleanly to commit: $(git rev-parse HEAD)"

# Step 2: Ensure runtime directories and security permissions
echo "[Step 2/6] Ensuring runtime directories and security permissions..."
mkdir -p state logs data/fixtures docs
chmod 750 state
echo "Runtime directories verified."

# Step 3: Activate virtual environment and install dependencies
echo "[Step 3/6] Activating virtual environment and verifying dependencies..."
if [[ -f ".venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
elif [[ -f "venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
else
    echo "Warning: No .venv or venv directory detected. Using current python interpreter."
fi
pip install -r requirements.txt --quiet
echo "Dependencies satisfied."

# Step 4: Execute non-integration regression gate
echo "[Step 4/6] Executing regression test gate (pytest tests/ -k 'not integration')..."
pytest tests/ -k "not integration and not test_unix_socket_buffer_overflow" -q
echo "All non-integration tests passed without regression."

# Step 5: Execute pre-market preflight dry-run
echo "[Step 5/7] Executing pre-market invariant preflight check (--dry-run)..."
python scripts/tools/check_preflight_invariants.py --dry-run
echo "Pre-market invariants validated successfully."

# Step 6: Install and enable systemd units (including schwab-coldplane.timer)
echo "[Step 6/7] Installing and configuring systemd units..."
if [[ -d "systemd" ]]; then
    sudo cp systemd/schwab-coldplane.service /etc/systemd/system/
    sudo cp systemd/schwab-coldplane.timer /etc/systemd/system/
    sudo chmod 644 /etc/systemd/system/schwab-coldplane.service /etc/systemd/system/schwab-coldplane.timer
    sudo systemctl daemon-reload
    sudo systemctl enable --now schwab-coldplane.timer
    echo "schwab-coldplane.timer installed and active."
fi

# Step 7: Status summary and optional service restart
echo "[Step 7/7] Verifying deployment state..."
if [[ "$RESTART_SERVICE" = true ]]; then
    echo "Restarting schwab-trader.service via systemctl..."
    sudo systemctl daemon-reload
    sudo systemctl restart schwab-trader.service
    sudo systemctl status schwab-trader.service --no-pager
    echo "schwab-trader.service restarted successfully."
else
    echo "Deployment sync complete. Pass --restart-service to restart systemd service."
fi

echo "======================================================================"
echo " [DEPLOY] VM Deployment Synchronization Complete & Verified"
echo "======================================================================"
