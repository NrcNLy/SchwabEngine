#!/bin/bash
# Runs ON the VM. Pulls the repository and redeploys the whole tree
# (main.py, governor.py, core/, services/, execution/, api/, config/, src/ -> dist/ via the image build).
#
#   bash deploy.sh                          # dry-run engine
#   ENGINE_FLAGS="--live" bash deploy.sh    # live trading
#
# Untracked host files (schwab_tokens_vault.json, .env, ~/schwab_state) are never touched by the pull.
set -euo pipefail

cd ~/schwab_engine

if [ -d .git ]; then
    echo "Pulling latest from origin..."
    git pull --ff-only
else
    echo "No .git directory here; deploying the files already on disk (use deploy.ps1 from your PC to upload)."
fi

bash remote_deploy.sh