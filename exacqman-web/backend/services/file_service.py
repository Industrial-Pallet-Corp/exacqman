"""
File Service

Handles file operations for processed videos in the ExacqMan web application.
"""

import os
import json
import shutil
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

from api.models import FileInfo

try:
    from mutagen.mp4 import MP4, MP4StreamInfoError
    _MUTAGEN_AVAILABLE = True
except ImportError:
    # Mutagen is the canonical reader for the JSON metadata blob the CLI
    # embeds in the mp4 `comment` tag. If it's not installed (e.g. older
    # checkouts of the backend that pre-date the dependency add), we
    # fall back to filename-based parsing so the file list still works
    # -- just without the "rename-config-survives" guarantee.
    MP4 = None  # type: ignore[assignment]
    MP4StreamInfoError = Exception  # type: ignore[misc,assignment]
    _MUTAGEN_AVAILABLE = False

logger = logging.getLogger(__name__)

# Module-level cache keyed by `(path, mtime)`: stat'ing every file in the
# exports directory on every /api/files call is cheap, but parsing the
# mp4 container atoms is not (and gets repeated unchanged across polls).
# Using mtime in the key means the cache invalidates automatically when
# a file is rewritten in place (which the CLI's atomic-replace pattern
# preserves). Bounded implicitly by MAX_EXPORT_FILES (=25) so we don't
# need an LRU eviction policy here.
_EmbeddedMetadata = Dict[str, Any]
_metadata_cache: Dict[str, Tuple[float, _EmbeddedMetadata]] = {}

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

            # Prefer metadata embedded in the mp4 container by the CLI: it
            # survives any config-file rotation or rename, and works for
            # files whose user-supplied `-o` filename doesn't follow the
            # canonical `{date}_{server}_{camera}_{multiplier}x` convention.
            # The filename parser is kept as a fallback so legacy files
            # (recorded before metadata embedding existed) still surface
            # camera + multiplier on the file list.
            embedded = self._read_embedded_metadata(file_path, stat.st_mtime)
            camera_alias = embedded.get("camera_alias")
            multiplier_raw = embedded.get("multiplier")
            timelapse_multiplier: Optional[int] = None
            if multiplier_raw is not None:
                try:
                    timelapse_multiplier = int(multiplier_raw)
                except (TypeError, ValueError):
                    timelapse_multiplier = None

            if camera_alias is None and timelapse_multiplier is None:
                # Legacy fallback: parse the filename.
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

    def _read_embedded_metadata(self, file_path: Path, mtime: float) -> _EmbeddedMetadata:
        """Read the JSON metadata blob embedded in an mp4's ``comment`` tag.

        The CLI embeds a versioned JSON payload (see
        ``EXACQMAN_METADATA_VERSION`` in ``exacqman.py``) carrying
        provenance: server, camera_alias, camera_id, multiplier,
        start/end ISO datetimes, timezone, and caption. That blob is the
        authoritative source for the file-browser's metadata columns;
        filename parsing is purely a backward-compat fallback.

        Cached by ``(path, mtime)`` so repeated polls of ``/api/files``
        don't re-parse the container on every call. The cache entry
        invalidates automatically the moment a file is rewritten.

        Returns an empty dict for any failure mode (mutagen missing,
        non-mp4 input, container parse error, malformed JSON, etc.) so
        callers can treat it as "no metadata available" without special
        branching.
        """
        if not _MUTAGEN_AVAILABLE:
            return {}
        if file_path.suffix.lower() != ".mp4":
            return {}

        cache_key = str(file_path)
        cached = _metadata_cache.get(cache_key)
        if cached is not None and cached[0] == mtime:
            return cached[1]

        result: _EmbeddedMetadata = {}
        try:
            mp4 = MP4(str(file_path))
            # The mp4 `comment` tag is stored under the iTunes-style
            # 4-char atom name `\xa9cmt`. Mutagen exposes it as a list
            # (multi-value atoms are legal in the spec); the CLI only
            # ever writes a single string, so we take the first entry.
            tags = mp4.tags or {}
            comment_values = tags.get("\xa9cmt") or []
            if comment_values:
                raw = comment_values[0]
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    result = payload
        except (MP4StreamInfoError, OSError, ValueError, json.JSONDecodeError) as exc:
            # MP4StreamInfoError: not a valid mp4 / missing moov atom.
            # OSError: permissions, vanished file, etc.
            # ValueError / JSONDecodeError: malformed payload from an
            # older or hand-edited file. None of these are fatal: we
            # log at debug (not warning) so the file list endpoint
            # stays quiet for the common "legacy file, no metadata"
            # case.
            logger.debug(f"No embedded metadata for {file_path.name}: {exc}")

        _metadata_cache[cache_key] = (mtime, result)
        return result

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