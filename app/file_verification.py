"""
File Verification Module
Validates downloaded video files for integrity and completeness
"""
import os
import subprocess
import json
import logging
from pathlib import Path
from typing import Dict, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Result of file verification"""
    is_valid: bool
    file_path: str
    file_size: int
    duration: Optional[float] = None
    format: Optional[str] = None
    video_codec: Optional[str] = None
    audio_codec: Optional[str] = None
    resolution: Optional[str] = None
    error: Optional[str] = None
    verified_at: str = None

    def __post_init__(self):
        if self.verified_at is None:
            self.verified_at = datetime.now().isoformat()


class FileVerifier:
    """Verifies integrity and validity of downloaded video files"""

    def __init__(self, ffprobe_path: str = 'ffprobe'):
        """
        Initialize file verifier

        Args:
            ffprobe_path: Path to ffprobe executable (default: 'ffprobe' from PATH)
        """
        self.ffprobe_path = ffprobe_path

    def verify_file(self, file_path: str, min_size_bytes: int = 1024) -> VerificationResult:
        """
        Verify a video file for integrity

        Args:
            file_path: Path to the video file
            min_size_bytes: Minimum acceptable file size (default: 1KB)

        Returns:
            VerificationResult with validation status and metadata
        """
        file_path = Path(file_path)

        # Check if file exists
        if not file_path.exists():
            return VerificationResult(
                is_valid=False,
                file_path=str(file_path),
                file_size=0,
                error="File does not exist"
            )

        # Check file size
        file_size = file_path.stat().st_size
        if file_size < min_size_bytes:
            return VerificationResult(
                is_valid=False,
                file_path=str(file_path),
                file_size=file_size,
                error=f"File too small ({file_size} bytes, minimum {min_size_bytes} bytes)"
            )

        # Use ffprobe to validate video file
        try:
            metadata = self._probe_file(str(file_path))

            if metadata is None:
                return VerificationResult(
                    is_valid=False,
                    file_path=str(file_path),
                    file_size=file_size,
                    error="Failed to probe file with ffprobe"
                )

            # Extract relevant information
            format_info = metadata.get('format', {})
            video_stream = self._get_video_stream(metadata)
            audio_stream = self._get_audio_stream(metadata)

            duration = float(format_info.get('duration', 0))
            format_name = format_info.get('format_name', 'unknown')

            # Check if file has valid duration
            if duration <= 0:
                return VerificationResult(
                    is_valid=False,
                    file_path=str(file_path),
                    file_size=file_size,
                    format=format_name,
                    error="Invalid or zero duration"
                )

            # Extract codec information
            video_codec = video_stream.get('codec_name', 'unknown') if video_stream else None
            audio_codec = audio_stream.get('codec_name', 'unknown') if audio_stream else None

            # Extract resolution
            resolution = None
            if video_stream:
                width = video_stream.get('width', 0)
                height = video_stream.get('height', 0)
                if width and height:
                    resolution = f"{width}x{height}"

            # File is valid if it has a valid duration and at least one stream
            is_valid = duration > 0 and (video_stream is not None or audio_stream is not None)

            return VerificationResult(
                is_valid=is_valid,
                file_path=str(file_path),
                file_size=file_size,
                duration=duration,
                format=format_name,
                video_codec=video_codec,
                audio_codec=audio_codec,
                resolution=resolution,
                error=None if is_valid else "No valid video or audio streams found"
            )

        except Exception as e:
            return VerificationResult(
                is_valid=False,
                file_path=str(file_path),
                file_size=file_size,
                error=f"Verification error: {str(e)}"
            )

    def _probe_file(self, file_path: str) -> Optional[Dict]:
        """
        Use ffprobe to extract file metadata

        Args:
            file_path: Path to the file

        Returns:
            Dictionary with file metadata or None on error
        """
        try:
            cmd = [
                self.ffprobe_path,
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_format',
                '-show_streams',
                file_path
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30  # 30 second timeout
            )

            if result.returncode != 0:
                return None

            return json.loads(result.stdout)

        except subprocess.TimeoutExpired:
            return None
        except FileNotFoundError:
            # ffprobe not found
            return None
        except json.JSONDecodeError:
            return None
        except Exception:
            return None

    def _get_video_stream(self, metadata: Dict) -> Optional[Dict]:
        """Get first video stream from metadata"""
        streams = metadata.get('streams', [])
        for stream in streams:
            if stream.get('codec_type') == 'video':
                return stream
        return None

    def _get_audio_stream(self, metadata: Dict) -> Optional[Dict]:
        """Get first audio stream from metadata"""
        streams = metadata.get('streams', [])
        for stream in streams:
            if stream.get('codec_type') == 'audio':
                return stream
        return None

    def quick_verify(self, file_path: str) -> bool:
        """
        Quick verification - just checks if file exists and has non-zero size

        Args:
            file_path: Path to the file

        Returns:
            True if file exists and has content
        """
        try:
            path = Path(file_path)
            return path.exists() and path.stat().st_size > 0
        except Exception:
            return False


def format_duration(seconds: float) -> str:
    """
    Format duration in seconds to human-readable string

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted string (e.g., "1h 23m 45s")
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


def format_file_size(bytes: int) -> str:
    """
    Format file size to human-readable string

    Args:
        bytes: File size in bytes

    Returns:
        Formatted string (e.g., "1.23 GB")
    """
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes < 1024.0:
            return f"{bytes:.2f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.2f} PB"


# Example usage
if __name__ == '__main__':
    import sys

    if len(sys.argv) < 2:
        logger.info("Usage: python file_verification.py <video_file>")
        sys.exit(1)

    verifier = FileVerifier()
    result = verifier.verify_file(sys.argv[1])

    logger.info(f"File: {result.file_path}")
    logger.info(f"Valid: {'Yes' if result.is_valid else 'No'}")
    logger.info(f"Size: {format_file_size(result.file_size)}")

    if result.duration:
        logger.info(f"Duration: {format_duration(result.duration)}")
    if result.format:
        logger.info(f"Format: {result.format}")
    if result.video_codec:
        logger.info(f"Video Codec: {result.video_codec}")
    if result.audio_codec:
        logger.info(f"Audio Codec: {result.audio_codec}")
    if result.resolution:
        logger.info(f"Resolution: {result.resolution}")
    if result.error:
        logger.error(f"Error: {result.error}")
