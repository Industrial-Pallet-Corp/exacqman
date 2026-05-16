"""
File Service

Handles file operations for processed videos in the ExacqMan web application.
"""

import os
import shutil
import logging
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from api.models import FileInfo

logger = logging.getLogger(__name__)

class FileService:
    """Service for handling processed video files."""
    
    def __init__(self):
        """Initialize the file service."""
        # exports directory is at the same level as backend directory
        self.exports_dir = Path("../exports")
        self.allowed_extensions = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv'}
        
        # Ensure exports directory exists
        self.exports_dir.mkdir(exist_ok=True)
    
    def get_processed_videos(self) -> List[FileInfo]:
        """
        Get list of processed video files in the exports directory.
        
        Returns:
            List of FileInfo objects for processed videos
        """
        try:
            video_files = []
            
            if not self.exports_dir.exists():
                return video_files
            
            for file_path in self.exports_dir.iterdir():
                if file_path.is_file() and self._is_allowed_file_type(file_path.name):
                    file_info = self._create_file_info(file_path)
                    video_files.append(file_info)
            
            # Sort by creation time (newest first)
            video_files.sort(key=lambda x: x.created_at, reverse=True)
            
            logger.info(f"Found {len(video_files)} processed video files")
            return video_files
            
        except Exception as e:
            logger.error(f"Error listing processed videos: {str(e)}")
            return []
    
    def get_file_path(self, filename: str) -> str:
        """
        Get the full path to a processed video file.
        
        Args:
            filename: Name of the file
            
        Returns:
            Full path to the file
            
        Raises:
            FileNotFoundError: If file is not found
        """
        file_path = self.exports_dir / filename
        if not file_path.exists():
            raise FileNotFoundError(f"Processed video not found: {filename}")
        
        return str(file_path)
    
    def delete_file(self, filename: str) -> bool:
        """
        Delete a processed video file.
        
        Args:
            filename: Name of the file to delete
            
        Returns:
            True if deleted successfully, False otherwise
        """
        try:
            file_path = Path(self.get_file_path(filename))
            file_path.unlink()
            logger.info(f"Processed video deleted successfully: {filename}")
            return True
        except Exception as e:
            logger.error(f"Error deleting processed video {filename}: {str(e)}")
            return False
    
    def get_file_size(self, file_path: str) -> int:
        """
        Get the size of a file in bytes.
        
        Args:
            file_path: Path to the file
            
        Returns:
            File size in bytes
        """
        try:
            return os.path.getsize(file_path)
        except OSError:
            return 0
    
    def cleanup_old_files(self, max_age_days: int = 30) -> int:
        """
        Clean up old processed video files.
        
        Args:
            max_age_days: Maximum age of files in days
            
        Returns:
            Number of files deleted
        """
        try:
            deleted_count = 0
            cutoff_time = datetime.now().timestamp() - (max_age_days * 24 * 60 * 60)
            
            for file_path in self.exports_dir.iterdir():
                if file_path.is_file() and self._is_allowed_file_type(file_path.name):
                    file_age = file_path.stat().st_mtime
                    if file_age < cutoff_time:
                        file_path.unlink()
                        deleted_count += 1
                        logger.info(f"Cleaned up old processed video: {file_path.name}")
            
            return deleted_count
            
        except Exception as e:
            logger.error(f"Error during file cleanup: {str(e)}")
            return 0
    
    def _is_allowed_file_type(self, filename: str) -> bool:
        """
        Check if a file has an allowed extension.
        
        Args:
            filename: Name of the file
            
        Returns:
            True if file type is allowed
        """
        file_extension = Path(filename).suffix.lower()
        return file_extension in self.allowed_extensions
    
    def _create_file_info(self, file_path: Path) -> FileInfo:
        """
        Create a FileInfo object from a file path.
        
        Args:
            file_path: Path to the file
            
        Returns:
            FileInfo object
        """
        try:
            stat = file_path.stat()
            
            # Try to extract metadata from filename
            camera_alias, timelapse_multiplier = self._parse_filename_metadata(file_path.name)
            
            return FileInfo(
                filename=file_path.name,
                path=str(file_path),
                size=stat.st_size,
                created_at=datetime.fromtimestamp(stat.st_ctime).isoformat(),
                modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
                file_type=file_path.suffix.lower(),
                camera_alias=camera_alias,
                timelapse_multiplier=timelapse_multiplier
            )
        except Exception as e:
            logger.error(f"Error creating file info for {file_path}: {str(e)}")
            # Return minimal file info
            return FileInfo(
                filename=file_path.name,
                path=str(file_path),
                size=0,
                created_at=datetime.now().isoformat(),
                modified_at=datetime.now().isoformat(),
                file_type=file_path.suffix.lower(),
                camera_alias=None,
                timelapse_multiplier=None
            )
    
    def _parse_filename_metadata(self, filename: str) -> tuple[Optional[str], Optional[int]]:
        """
        Parse camera alias and timelapse multiplier from filename.

        Expected format: ``YYYY-MM-DD_HHMM_server_camera_multiplierx.mp4``
        (HHMM is 24-hour). Parsing keys off the last two underscore-separated
        parts, so it tolerates extra leading components such as the server
        slot added by the new filename convention.

        Args:
            filename: Name of the file

        Returns:
            Tuple of (camera_alias, timelapse_multiplier)
        """
        try:
            # Remove extension
            name_without_ext = Path(filename).stem
            
            # Split by underscore
            parts = name_without_ext.split('_')
            
            if len(parts) >= 4:
                # parts[-1] = multiplier (e.g. "50x"); parts[-2] = camera alias
                
                # Last part should be the multiplier (e.g., "10x")
                multiplier_part = parts[-1]
                if multiplier_part.endswith('x'):
                    try:
                        multiplier = int(multiplier_part[:-1])
                    except ValueError:
                        multiplier = None
                else:
                    multiplier = None
                
                # Camera alias is the second-to-last part
                camera_alias = parts[-2] if multiplier is not None else None
                
                return camera_alias, multiplier
            
            return None, None
            
        except Exception as e:
            logger.error(f"Error parsing filename metadata from {filename}: {str(e)}")
            return None, None