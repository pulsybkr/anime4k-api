# Anime4K Web Upscaler — Design Spec

## Purpose

Single-user web interface for drag-and-drop video upscaling using Anime4KCPP on a Linux GPU server. Two modes: HD (1080p) and UHD 4K.

## Architecture

```
Browser (drag & drop video)
    │
    ▼
FastAPI (Python) — handles upload, launches processing, serves results
    │
    ▼
Anime4KCPP (ac_cli + CUDA) — applies Anime4K shaders on GPU
    │
    ▼
FFmpeg — video decode/encode
```

## Processing Pipeline

1. Video uploaded via drag-and-drop, saved to `uploads/`
2. FastAPI spawns `ac_cli` with mode-appropriate scale parameters
3. `ac_cli` processes via FFmpeg + CUDA on GPU
4. Upscaled video lands in `outputs/`
5. Download link displayed in the UI

## Modes

| Mode | Target | Behavior |
|------|--------|----------|
| HD 1080p | 1920x1080 | Scale to 1080p (if source < 1080p, upscale; if higher, downscale) |
| UHD 4K | 3840x2160 | 2x upscale for 1080p sources, 4x for SD sources |

## Web Interface

Single page, dark theme:
- Centered drop zone with HD/4K toggle
- Upload + processing progress bar
- Download link with thumbnail preview on completion

## API Endpoints

- `POST /upload` — receives video + mode, returns task_id, starts background processing
- `GET /status/{task_id}` — progress polling (percent + stage)
- `GET /download/{task_id}` — serves the processed video file
- `GET /health` — checks GPU, ac_cli, ffmpeg availability

## Startup Dependency Checks

Server checks on boot and reports clear errors if:
- `ac_cli` not found (not compiled/installed)
- CUDA drivers / `nvidia-smi` missing
- `ffmpeg` missing
- Insufficient disk space in working directories

## File Structure

```
anime4k-web/
├── server.py          # FastAPI app, routes, processing logic
├── requirements.txt   # fastapi, uvicorn, python-multipart, aiofiles
├── static/
│   └── index.html     # Frontend (HTML + CSS + JS, single file)
├── uploads/           # Temp uploaded videos
├── outputs/           # Processed output videos
└── install.sh         # Anime4KCPP build + system deps setup
```

## Error Handling

- Processing failures: surfaced to UI with specific error message
- File cleanup: uploaded files deleted after 1h, outputs after 24h (via background task)
- File size limit: 5GB max upload

## Non-Goals

- No authentication or user accounts
- No job queue (single-user, one job at a time)
- No mobile-first design (targeted at desktop usage)
