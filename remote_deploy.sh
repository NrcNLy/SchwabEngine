#!/bin/bash
set -e

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

echo 'Building Docker image...'
sudo docker build -t schwab_engine .

echo 'Stopping existing container...'
sudo docker stop schwab_engine_container 2>/dev/null || true
sudo docker rm schwab_engine_container 2>/dev/null || true

echo 'Starting new container...'
sudo docker run -d \
    --name schwab_engine_container \
    --restart unless-stopped \
    -v $(pwd)/schwab_tokens_vault.json:/app/schwab_tokens_vault.json \
    --env-file $(pwd)/.env \
    schwab_engine

echo 'Deployment complete. Container is running.'
sudo docker ps | grep schwab_engine_container
