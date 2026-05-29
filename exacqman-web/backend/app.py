"""
ExacqMan Web Server - FastAPI Application

A web interface for the ExacqMan video processing tool that provides REST API endpoints
for video extraction, compression, and timelapse operations.
"""

from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn
import os
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any
import json
import logging
from datetime import datetime

from api.routes import router, job_queue
from services.exacqman_service import ExacqManService
from services.file_service import FileService

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

# Mount static files for serving processed videos
app.mount("/exports", StaticFiles(directory="exports"), name="exports")

# Mount frontend files (this should be LAST to catch all other routes)
app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")

# Initialize services
exacqman_service = ExacqManService()
file_service = FileService()

@app.on_event("startup")
async def startup_event():
    """Initialize the application on startup."""
    logger.info("Starting ExacqMan Web Server...")

    # Ensure exports directory exists
    os.makedirs("exports", exist_ok=True)

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
        "app:app",
        host="0.0.0.0",
        port=8887,
        reload=True,
        log_level="info"
    )
