"""
ExacqMan Web Server - FastAPI Application

A web interface for the ExacqMan video processing tool that provides REST API endpoints
for video extraction, compression, and timelapse operations.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
import asyncio
import importlib.resources
import logging
from datetime import datetime
from pathlib import Path

from exacqman import paths
from exacqman.web.api.routes import router, job_queue

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Exports are user data, resolved via exacqman.paths (the cwd's exports/ for a
# foreground server, or EXACQMAN_EXPORTS_DIR for a managed service) so a
# read-only installed package never writes into its own tree. The frontend is
# bundled, read-only static content shipped inside the package; locate it via
# importlib.resources so it resolves correctly once installed into a venv.
EXPORTS_DIR = paths.exports_dir()
FRONTEND_DIR = Path(importlib.resources.files("exacqman.web") / "frontend")

# Create FastAPI app
app = FastAPI(
    title="ExacqMan Web API",
    description="Web interface for ExacqMan video processing tool",
    version="1.0.0"
)

# Add CORS middleware for frontend communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify actual frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes FIRST (before static file mounts)
app.include_router(router, prefix="/api")

# Health check endpoints
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/api/health")
async def api_health_check():
    """API health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

# Ensure the exports directory exists before mounting it (StaticFiles validates
# the directory at mount time).
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Mount static files for serving processed videos
app.mount("/exports", StaticFiles(directory=str(EXPORTS_DIR)), name="exports")

# Mount frontend files (this should be LAST to catch all other routes)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

@app.on_event("startup")
async def startup_event():
    """Initialize the application on startup."""
    logger.info("Starting ExacqMan Web Server...")

    # Spin up the serial job worker. Subsequent /api/extract calls will
    # enqueue against this single instance, ensuring one-at-a-time
    # processing across all clients.
    await job_queue.start()

    logger.info("ExacqMan Web Server started successfully")

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on application shutdown.

    Bounded so a wedged worker can never block the FastAPI lifespan from
    completing -- uvicorn waits for this to return before it releases the
    port, so taking longer than a few seconds here is the difference
    between "kill -TERM and it's gone" and "kill -TERM and you wait".
    """
    logger.info("Shutting down ExacqMan Web Server...")
    try:
        await asyncio.wait_for(job_queue.stop(), timeout=8.0)
    except asyncio.TimeoutError:
        logger.warning(
            "JobQueue.stop() did not complete within 8s; proceeding with shutdown anyway"
        )
    logger.info("ExacqMan Web Server shut down")

@app.get("/")
async def root():
    """Root endpoint with API information."""
    return {
        "message": "ExacqMan Web API",
        "version": "1.0.0",
        "endpoints": {
            "extract": "/api/extract",
            "jobs": "/api/jobs",
            "files": "/api/files",
            "download": "/api/download/{filename}",
            "config": "/api/config/{config_file}",
            "cameras": "/api/cameras/{config_file}"
        }
    }

if __name__ == "__main__":
    uvicorn.run(
        "exacqman.web.app:app",
        host="0.0.0.0",
        port=8887,
        reload=True,
        log_level="info"
    )
