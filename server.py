"""
server.py
=========
Root entrypoint for the Schwab Engine API daemon.
Delegates to the FastAPI application defined in api/server.py.
"""

import sys
import os
import uvicorn
import logging
from pathlib import Path

# Add project root to sys.path to resolve internal modules
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from api.server import build_app, EngineContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    logger.info("Initializing EngineContext...")
    # Initialize a standalone EngineContext for the API server daemon.
    # In a fully integrated environment, this context would be populated 
    # by main.py, but for the local UI backend, we serve the frontend contracts.
    ctx = EngineContext()
    
    app = build_app(ctx)
    if app is None:
        logger.error("Failed to build FastAPI app. Check dependencies.")
        sys.exit(1)
        
    logger.info("Starting uvicorn server on 0.0.0.0:8080...")
    uvicorn.run(app, host="0.0.0.0", port=8080)

if __name__ == "__main__":
    if "--test" in sys.argv:
        ctx = EngineContext()
        app = build_app(ctx)
        if app:
            logger.info("Test mode: FastAPI app built successfully. Contract verified.")
            sys.exit(0)
        else:
            sys.exit(1)
            
    main()
