"""
API Routes for ExacqMan Web Server

Defines REST API endpoints for video processing operations.
"""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from typing import List, Optional
import logging
from datetime import datetime
import os

from api.models import (
    ExtractRequest, CameraInfo, ConfigInfo,
    JobsSnapshot, FileInfo, ApiResponse,
)
from services.exacqman_service import ExacqManService
from services.file_service import FileService
from services.config_service import ConfigService
from services.job_queue import JobQueue, BacklogFullError

logger = logging.getLogger(__name__)

router = APIRouter()

# Initialize services
exacqman_service = ExacqManService()
file_service = FileService()
config_service = ConfigService()

# Single shared serial job queue. The worker task is started/stopped via
# the FastAPI lifespan hooks in app.py.
job_queue = JobQueue(run_extract=exacqman_service.extract_video_with_progress)


@router.post("/extract", response_model=ApiResponse)
async def extract_video(request: ExtractRequest) -> ApiResponse:
    """
    Enqueue a video extraction job.

    The job is appended to the shared serial queue and runs once any
    predecessors finish. Backlog is bounded; once ``MAX_WAITING`` jobs are
    already waiting the request is rejected with HTTP 429.
    """
    try:
        if not config_service.validate_camera(request.config_file, request.camera_alias):
            raise HTTPException(
                status_code=400,
                detail=f"Camera '{request.camera_alias}' not found in configuration",
            )

        try:
            job = await job_queue.submit(request)
        except BacklogFullError as e:
            logger.info(f"Extract submission rejected: {e}")
            raise HTTPException(status_code=429, detail=str(e))

        logger.info(f"Extract job {job.id} queued for camera {request.camera_alias}")

        return ApiResponse(
            success=True,
            message="Extract job queued successfully",
            data={"job_id": job.id},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating extract job: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create extract job: {str(e)}")


@router.get("/jobs", response_model=JobsSnapshot)
async def get_jobs_snapshot(
    since: Optional[datetime] = Query(
        None,
        description=(
            "Only include terminal jobs whose completed_at is strictly after "
            "this timestamp. Pass the server_time from your previous poll to "
            "see each transition exactly once. Omit on a fresh page load to "
            "skip pre-existing terminal jobs entirely."
        ),
    )
) -> JobsSnapshot:
    """
    Single snapshot of the server queue.

    Clients poll this endpoint on a short cadence. The response shape
    includes the currently-running job, the FIFO waiting list, and a tail
    of recently-terminal jobs filtered by ``since``.
    """
    # ``since=None`` on a fresh load would let every terminal job leak
    # through, defeating the "only clients that observed it see it" rule.
    # We default to "now" so a missing ``since`` is treated as "I just
    # opened the page".
    effective_since = since if since is not None else datetime.now()
    return await job_queue.snapshot(since=effective_since)


@router.get("/files", response_model=List[FileInfo])
async def list_processed_videos() -> List[FileInfo]:
    """
    List all processed video files.

    Returns:
        List of FileInfo objects for processed videos
    """
    try:
        files = file_service.get_processed_videos()
        return files

    except Exception as e:
        logger.error(f"Error listing processed videos: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to list files: {str(e)}")


@router.get("/download/{filename}")
async def download_video(filename: str):
    """
    Download a processed video file.

    Args:
        filename: Name of the file to download

    Returns:
        FileResponse with the video file
    """
    try:
        file_path = file_service.get_file_path(filename)

        if not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail="File not found")

        return FileResponse(
            path=file_path,
            filename=filename,
            media_type="video/mp4",
        )

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as e:
        logger.error(f"Error downloading file {filename}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to download file: {str(e)}")


@router.get("/configs", response_model=List[str])
async def get_available_configs() -> List[str]:
    """
    Get list of available configuration files.

    Returns:
        List of configuration file names
    """
    try:
        config_files = config_service.get_available_config_files()
        return config_files
    except Exception as e:
        logger.error(f"Error getting available configs: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to get available configurations")


@router.get("/config/{config_file}", response_model=ConfigInfo)
async def get_config_info(config_file: str) -> ConfigInfo:
    """
    Get configuration information including available cameras and servers.

    Args:
        config_file: Path to the configuration file

    Returns:
        ConfigInfo with available cameras, servers, and options
    """
    try:
        config_info = config_service.get_config_info(config_file)
        return config_info

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Configuration file not found: {config_file}")
    except Exception as e:
        logger.error(f"Error getting config info: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get configuration: {str(e)}")


@router.get("/cameras/{config_file}", response_model=List[CameraInfo])
async def get_cameras(config_file: str) -> List[CameraInfo]:
    """
    Get list of available cameras from configuration file.

    Args:
        config_file: Path to the configuration file

    Returns:
        List of CameraInfo objects
    """
    try:
        cameras = config_service.get_available_cameras(config_file)
        return cameras

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Configuration file not found: {config_file}")
    except Exception as e:
        logger.error(f"Error getting cameras: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to get cameras: {str(e)}")


@router.delete("/files/{filename}", response_model=ApiResponse)
async def delete_video(filename: str) -> ApiResponse:
    """
    Delete a processed video file.

    Args:
        filename: Name of the file to delete

    Returns:
        ApiResponse indicating success or failure
    """
    try:
        success = file_service.delete_file(filename)

        if success:
            return ApiResponse(
                success=True,
                message=f"File '{filename}' deleted successfully",
            )
        else:
            raise HTTPException(status_code=500, detail="Failed to delete file")

    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except Exception as e:
        logger.error(f"Error deleting file {filename}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(e)}")
