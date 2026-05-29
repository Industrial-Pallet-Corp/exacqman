# ExacqMan Web Server

A FastAPI-based web server that provides REST API endpoints for the ExacqMan video processing tool.

## Features

- **Video Extraction**: Extract videos from Exacqvision servers with timelapse and compression
- **Video Compression**: Compress existing video files to different quality levels
- **Timelapse Creation**: Create timelapse videos from existing files
- **File Management**: Upload, download, and manage video files
- **Background Processing**: Long-running operations are processed asynchronously
- **Progress Tracking**: Real-time status updates for processing jobs

## API Endpoints

### Video Processing
- `POST /api/extract` - Extract video from Exacqvision server
- `POST /api/compress` - Compress an existing video file
- `POST /api/timelapse` - Create timelapse from existing video

### File Management
- `POST /api/upload` - Upload video files
- `GET /api/files` - List available video files
- `GET /api/download/{filename}` - Download processed videos

### Job Management
- `GET /api/status/{job_id}` - Check job status and progress

### System
- `GET /` - API information
- `GET /health` - Health check

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Ensure ExacqMan is available in the parent directory

3. Start the server:
```bash
python exacqman_web.py start          # foreground (Ctrl-C to stop)
python exacqman_web.py start -b       # background (detached; logs -> ../logs/server.log)
```

The server will start on `http://localhost:8887`. `start` is the default
subcommand, so bare `python exacqman_web.py` also works.

Stop or check it from any terminal:
```bash
python exacqman_web.py stop           # graceful SIGTERM, then SIGKILL after 15s; frees the port
python exacqman_web.py status         # running / not running + PID, host, port
```

## API Documentation

Once the server is running, visit:
- Interactive API docs: `http://localhost:8887/docs`
- ReDoc documentation: `http://localhost:8887/redoc`

## Configuration

The server uses the same configuration files as the ExacqMan CLI tool. Make sure your config files are in the ExacqMan root directory.

## Development

The server is built with FastAPI and includes:
- Automatic API documentation
- Request/response validation with Pydantic
- Async processing for long-running operations
- CORS support for frontend integration
- Comprehensive error handling and logging
