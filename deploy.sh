#!/bin/bash
set -e
pip install fastapi uvicorn --quiet -q

docker cp ~/schwab_engine/core/auth.py schwab_engine_container:/app/core/auth.py
docker cp ~/schwab_engine/config/config.yaml schwab_engine_container:/app/config/config.yaml

docker exec schwab_engine_container mkdir -p /app/news /app/portfolio /app/reconciliation /app/research /app/api

docker cp ~/schwab_engine/news/ schwab_engine_container:/app/
docker cp ~/schwab_engine/portfolio/ schwab_engine_container:/app/
docker cp ~/schwab_engine/reconciliation/ schwab_engine_container:/app/
docker cp ~/schwab_engine/research/ schwab_engine_container:/app/
docker cp ~/schwab_engine/api/ schwab_engine_container:/app/

docker restart schwab_engine_container
sleep 7
docker logs --tail 25 schwab_engine_container
echo "=== MEM ==="
free -m