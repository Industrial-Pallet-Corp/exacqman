"""
Data models for ExacqMan Web API

Defines Pydantic models for request/response validation and serialization.
"""

from pydantic import BaseModel, Field, validator
from typing import Optional, Dict, Any, List, Union
from datetime import datetime
from enum import Enum

class QualityLevel(str, Enum):
    """Video quality levels for compression."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class JobStatusEnum(str, Enum):
    """Job status enumeration."""
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

class ExtractRequest(BaseModel):
    """Request model for video extraction."""
    camera_alias: str = Field(..., description="Camera alias from config")
    start_datetime: Union[datetime, str] = Field(..., description="Start datetime for video extraction")
    end_datetime: Union[datetime, str] = Field(..., description="End datetime for video extraction")
    timelapse_multiplier: int = Field(10, description="Timelapse multiplier (1-50)")
    config_file: str = Field(..., description="Path to config file")
    server: Optional[str] = Field(None, description="Server location initials")
    caption: Optional[str] = Field(
        None,
        max_length=30,
        description="Optional caption rendered below the timestamp (max 30 chars)",
    )
    filename: Optional[str] = Field(
        None,
        max_length=30,
        description=(
            "Optional output filename stem (no extension). When omitted, the "
            "service generates one of the form "
            "{date}_{time}_{server}_{camera}_{multiplier}x."
        ),
    )

    @validator('timelapse_multiplier')
    def validate_multiplier(cls, v):
        if not (1 <= v <= 50):
            raise ValueError('Timelapse multiplier must be between 1 and 50')
        return v

    @validator('caption', pre=True)
    def normalize_caption(cls, v):
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError('Caption must be a string')
        return v if v.strip() else None

    @validator('filename', pre=True)
    def normalize_filename(cls, v):
        if v is None:
            return None
        if not isinstance(v, str):
            raise ValueError('Filename must be a string')
        # Strip whitespace and any user-supplied extension; the service
        # always writes .mp4. Reject paths so input cannot escape the
        # exports directory.
        stripped = v.strip()
        if not stripped:
            return None
        if '/' in stripped or '\\' in stripped:
            raise ValueError('Filename must not contain path separators')
        # Drop a single trailing extension if present (e.g. ".mp4").
        if '.' in stripped:
            stripped = stripped.rsplit('.', 1)[0]
        return stripped or None
    
    @validator('start_datetime', pre=True)
    def parse_start_datetime(cls, v):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace('Z', '+00:00'))
            except ValueError:
                raise ValueError('Invalid start datetime format. Use ISO format (YYYY-MM-DDTHH:MM)')
        return v
    
    @validator('end_datetime', pre=True)
    def parse_end_datetime(cls, v):
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v.replace('Z', '+00:00'))
            except ValueError:
                raise ValueError('Invalid end datetime format. Use ISO format (YYYY-MM-DDTHH:MM)')
        return v
    
    @validator('end_datetime')
    def validate_end_after_start(cls, v, values):
        if 'start_datetime' in values and v <= values['start_datetime']:
            raise ValueError('End datetime must be after start datetime')
        return v

class ProcessedVideo(BaseModel):
    """Model for processed video files."""
    filename: str = Field(..., description="Video filename")
    camera_alias: str = Field(..., description="Camera that recorded the video")
    created_at: datetime = Field(..., description="When the video was processed")
    file_size: int = Field(..., description="File size in bytes")
    timelapse_multiplier: int = Field(..., description="Timelapse speed multiplier")
    duration: Optional[str] = Field(None, description="Video duration (HH:MM:SS)")
    file_path: str = Field(..., description="Full path to the file")

class CameraInfo(BaseModel):
    """Model for camera information."""
    alias: str = Field(..., description="Camera alias")
    id: str = Field(..., description="Camera ID")
    server: Optional[str] = Field(None, description="Name of the server this camera lives under")
    description: Optional[str] = Field(None, description="Camera description")

class ConfigInfo(BaseModel):
    """Model for configuration information."""
    cameras: List[CameraInfo] = Field(..., description="Available cameras")
    servers: Dict[str, str] = Field(..., description="Available servers")
    timelapse_options: List[int] = Field(..., description="Available timelapse multipliers")

class JobStatus(BaseModel):
    """Job status response model."""
    job_id: str = Field(..., description="Unique job identifier")
    status: JobStatusEnum = Field(..., description="Current job status")
    progress: int = Field(..., description="Progress percentage (0-100)")
    message: str = Field(..., description="Status message")
    created_at: str = Field(..., description="Job creation timestamp")
    completed_at: Optional[str] = Field(None, description="Job completion timestamp")
    result: Optional[Dict[str, Any]] = Field(None, description="Job result data")

class FileInfo(BaseModel):
    """File information model for processed videos."""
    filename: str = Field(..., description="File name")
    path: str = Field(..., description="File path")
    size: int = Field(..., description="File size in bytes")
    created_at: str = Field(..., description="File creation timestamp")
    modified_at: str = Field(..., description="File modification timestamp")
    file_type: str = Field(..., description="File type/extension")
    camera_alias: Optional[str] = Field(None, description="Camera that recorded the video")
    timelapse_multiplier: Optional[int] = Field(None, description="Timelapse speed multiplier")

class ApiResponse(BaseModel):
    """Generic API response model."""
    success: bool = Field(..., description="Operation success status")
    message: str = Field(..., description="Response message")
    data: Optional[Dict[str, Any]] = Field(None, description="Response data")
    error: Optional[str] = Field(None, description="Error message if failed")

class UploadResponse(BaseModel):
    """File upload response model."""
    filename: str = Field(..., description="Uploaded file name")
    path: str = Field(..., description="File path on server")
    size: int = Field(..., description="File size in bytes")
    message: str = Field(..., description="Upload status message")
