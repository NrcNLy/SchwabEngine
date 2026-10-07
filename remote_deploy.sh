#!/bin/bash
# Runs ON the VM from ~/schwab_engine. Builds the image (which also builds the dashboard), runs the
# pre-flight inside the image, and only then replaces the running container.
#
#   bash remote_deploy.sh                       # dry-run engine (default, safe)
#   ENGINE_FLAGS="--live" bash remote_deploy.sh # live trading (pre-flight runs with --live)
#
set -euo pipefail

ENGINE_FLAGS="${ENGINE_FLAGS:-}"
PREFLIGHT_FLAGS=""
if [[ "$ENGINE_FLAGS" == *"--live"* ]]; then
    PREFLIGHT_FLAGS="--live"
fi

echo "Updating packages..."
if ! command -v docker &> /dev/null
then
    echo 'Docker not found. Installing Docker...'
    sudo apt-get update
    sudo apt-get install -y docker.io
    sudo systemctl enable docker
    sudo systemctl start docker
fi

cd ~/schwab_engine

# Ensure the vault file exists on the host so Docker mounts it as a file
touch schwab_tokens_vault.json
# Runtime state (policy, ingested documents, NLV anchors) lives in a mounted DIRECTORY
mkdir -p "$HOME/schwab_state"

echo 'Building Docker image (engine + dashboard)...'
sudo docker build -t schwab_engine .

MOUNTS=(
    -v "$(pwd)/schwab_tokens_vault.json:/app/schwab_tokens_vault.json"
    -v "$HOME/schwab_state:/app/state"
    --env-file "$(pwd)/.env"
    -e ENGINE_STATE_DIR=/app/state
)

echo "Running pre-flight (${PREFLIGHT_FLAGS:-dry-run checks only})..."
sudo docker run --rm "${MOUNTS[@]}" --entrypoint python schwab_engine scripts/preflight.py $PREFLIGHT_FLAGS

echo 'Stopping existing container...'
sudo docker stop schwab_engine_container 2>/dev/null || true
sudo docker rm schwab_engine_container 2>/dev/null || true

echo "Starting new container (engine flags: '${ENGINE_FLAGS}')..."
# The API has no authentication: publish it on the VM's loopback only and reach it through an
# SSH tunnel:  gcloud compute ssh schwab-trader --zone=us-central1-a -- -L 8080:localhost:8080
sudo docker run -d \
    --name schwab_engine_container \
    --restart unless-stopped \
    -p 127.0.0.1:8080:8080 \
    "${MOUNTS[@]}" \
    schwab_engine $ENGINE_FLAGS

echo 'Deployment complete. Container is running.'
sudo docker ps | grep schwab_engine_container
sleep 6
sudo docker logs --tail 25 schwab_engine_container
