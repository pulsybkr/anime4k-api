# Anime4K Web Upscaler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Single-user web app for drag-and-drop video upscaling with Anime4KCPP on Linux GPU.

**Architecture:** FastAPI serves a single-page HTML frontend. Uploaded videos are processed by `ac_cli` (Anime4KCPP) using CUDA GPU acceleration, then re-encoded via FFmpeg. Progress polling via `GET /status/{task_id}`, result via `GET /download/{task_id}`.

**Tech Stack:** Python 3.10+, FastAPI, uvicorn, python-multipart, aiofiles, vanilla HTML/CSS/JS

---

### Task 1: Project skeleton and dependencies

**Files:**
- Create: `requirements.txt`

- [ ] **Step 1: Write requirements.txt**

```txt
fastapi==0.115.6
uvicorn[standard]==0.34.0
python-multipart==0.0.19
aiofiles==24.1.0
```

- [ ] **Step 2: Create virtualenv and install**

Run: `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`

- [ ] **Step 3: Create empty directories**

Run: `mkdir -p static uploads outputs`

---

### Task 2: Dependency checker module

**Files:**
- Create: `server.py`

- [ ] **Step 1: Write the dependency checker in server.py**

```python
import subprocess
import shutil
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("anime4k-web")

def check_dependency(name: str, exe: str) -> str | None:
    path = shutil.which(exe)
    if path is None:
        return f"{name} ({exe}) not found. Please install it first."
    logger.info(f"{name} found: {path}")
    return None

def check_gpu() -> str | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return "nvidia-smi returned error. Check CUDA drivers."
        gpu = result.stdout.strip()
        logger.info(f"GPU: {gpu}")
        return None
    except FileNotFoundError:
        return "nvidia-smi not found. CUDA drivers may not be installed."
    except Exception as e:
        return f"GPU check failed: {e}"

def check_disk_space() -> str | None:
    for d, label in [("uploads", "uploads"), ("outputs", "outputs"), (".", "workspace")]:
        try:
            stat = os.statvfs(d) if os.path.exists(d) else os.statvfs(".")
            free_gb = (stat.f_frsize * stat.f_bavail) / (1024 ** 3)
            if free_gb < 2:
                return f"Low disk space in {label}/: {free_gb:.1f} GB free (need 2+ GB)"
        except Exception as e:
            return f"Cannot check disk space for {label}/: {e}"
    return None

def run_health_checks() -> dict:
    errors = []
    for name, exe in [("Anime4KCPP", "ac_cli"), ("FFmpeg", "ffmpeg")]:
        err = check_dependency(name, exe)
        if err:
            errors.append(err)
    gpu_err = check_gpu()
    if gpu_err:
        errors.append(gpu_err)
    disk_err = check_disk_space()
    if disk_err:
        errors.append(disk_err)
    return {"ok": len(errors) == 0, "errors": errors}
```

- [ ] **Step 2: Verify it runs**

Run: `python -c "from server import run_health_checks; print(run_health_checks())"`

Expected: prints health dict with errors (since ac_cli not installed on dev machine) or ok

---

### Task 3: FastAPI app with health and upload endpoints

**Files:**
- Modify: `server.py` — append FastAPI setup below existing code

- [ ] **Step 1: Add imports and FastAPI app initialization**

Append to `server.py`:

```python
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import asyncio
import uuid
import time
import json
from pathlib import Path

app = FastAPI(title="Anime4K Web Upscaler")

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
STATUS_DIR = Path("status")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
STATUS_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 5 * 1024 * 1024 * 1024  # 5 GB

# In-memory job tracking
jobs: dict[str, dict] = {}
```

- [ ] **Step 2: Add health endpoint**

Append to `server.py`:

```python
@app.get("/health")
async def health():
    return run_health_checks()
```

- [ ] **Step 3: Add upload endpoint**

Append to `server.py`:

```python
@app.post("/upload")
async def upload_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    mode: str = Form("hd"),
):
    if mode not in ("hd", "4k"):
        raise HTTPException(400, "Mode must be 'hd' or '4k'")

    task_id = uuid.uuid4().hex[:12]
    ext = Path(file.filename).suffix or ".mp4"
    safe_name = f"{task_id}{ext}"
    input_path = UPLOAD_DIR / safe_name

    # Read in chunks to respect max size
    total = 0
    with open(input_path, "wb") as f:
        while chunk := await file.read(8 * 1024 * 1024):
            total += len(chunk)
            if total > MAX_FILE_SIZE:
                input_path.unlink(missing_ok=True)
                raise HTTPException(413, "File exceeds 5 GB limit")
            f.write(chunk)

    jobs[task_id] = {
        "status": "uploaded",
        "progress": 0,
        "mode": mode,
        "filename": file.filename,
        "output": None,
        "error": None,
    }

    background_tasks.add_task(process_video, task_id, str(input_path), mode)
    return {"task_id": task_id}
```

- [ ] **Step 4: Verify upload endpoint works**

Run: `uvicorn server:app --port 8000 &`
Run: `curl -X POST http://localhost:8000/upload -F "file=@small_test.mp4" -F "mode=hd"`
Expected: `{"task_id":"abc123..."}`

---

### Task 4: Video processing logic

**Files:**
- Modify: `server.py` — append `process_video()` function

- [ ] **Step 1: Write the process_video function**

Append to `server.py`:

```python
async def process_video(task_id: str, input_path: str, mode: str):
    try:
        jobs[task_id]["status"] = "processing"
        jobs[task_id]["progress"] = 5

        output_filename = f"{task_id}_upscaled.mp4"
        output_path = str(OUTPUT_DIR / output_filename)

        width = 1920 if mode == "hd" else 3840
        height = 1080 if mode == "hd" else 2160

        jobs[task_id]["progress"] = 10

        cmd = [
            "ac_cli",
            "-i", input_path,
            "-o", output_path,
            "-s", str(width), str(height),
            "--gpu", "0",
        ]

        logger.info(f"Running: {' '.join(cmd)}")
        jobs[task_id]["progress"] = 15

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Poll progress — ac_cli can take minutes
        start = time.time()
        estimated_seconds = 120 if mode == "hd" else 300

        while True:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
                break
            except asyncio.TimeoutError:
                elapsed = time.time() - start
                pct = min(15 + int(80 * (elapsed / estimated_seconds)), 95)
                jobs[task_id]["progress"] = pct

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode()[:500] if stderr else "Unknown error"
            logger.error(f"ac_cli failed: {error_msg}")
            jobs[task_id]["status"] = "failed"
            jobs[task_id]["error"] = f"Processing failed: {error_msg}"
            return

        jobs[task_id]["status"] = "done"
        jobs[task_id]["progress"] = 100
        jobs[task_id]["output"] = output_filename
        logger.info(f"Task {task_id}: done -> {output_filename}")

    except Exception as e:
        logger.error(f"Task {task_id} unexpected error: {e}")
        jobs[task_id]["status"] = "failed"
        jobs[task_id]["error"] = str(e)
```

- [ ] **Step 2: Verify processing runs**

Run (manually): start a file upload, check logs for `ac_cli` invocation

---

### Task 5: Status and download endpoints + cleanup

**Files:**
- Modify: `server.py` — append endpoints

- [ ] **Step 1: Add status endpoint**

Append to `server.py`:

```python
@app.get("/status/{task_id}")
async def job_status(task_id: str):
    job = jobs.get(task_id)
    if job is None:
        raise HTTPException(404, "Task not found")
    return {
        "status": job["status"],
        "progress": job["progress"],
        "filename": job.get("filename"),
        "error": job.get("error"),
    }
```

- [ ] **Step 2: Add download endpoint**

Append to `server.py`:

```python
@app.get("/download/{task_id}")
async def download_video(task_id: str):
    job = jobs.get(task_id)
    if job is None:
        raise HTTPException(404, "Task not found")

    status = job["status"]
    if status == "failed":
        raise HTTPException(500, job.get("error", "Processing failed"))
    if status != "done":
        raise HTTPException(425, f"Not ready (status: {status})")

    output_path = OUTPUT_DIR / job["output"]
    if not output_path.exists():
        raise HTTPException(404, "Output file missing — may have been cleaned up")

    return FileResponse(
        str(output_path),
        media_type="video/mp4",
        filename=job.get("filename", "upscaled.mp4"),
    )
```

- [ ] **Step 3: Add cleanup background task**

Append to `server.py`:

```python
async def cleanup_old_files():
    while True:
        await asyncio.sleep(3600)  # every hour
        now = time.time()
        for d, max_age, label in [
            (UPLOAD_DIR, 3600, "uploads"),
            (OUTPUT_DIR, 86400, "outputs"),
        ]:
            for f in d.iterdir():
                if f.is_file() and (now - f.stat().st_mtime) > max_age:
                    f.unlink(missing_ok=True)
                    logger.info(f"Cleanup {label}: removed {f.name}")
```

- [ ] **Step 4: Add startup event**

Append to `server.py`:

```python
@app.on_event("startup")
async def startup():
    asyncio.create_task(cleanup_old_files())
    health = run_health_checks()
    if not health["ok"]:
        logger.warning("Health check FAILED:")
        for err in health["errors"]:
            logger.warning(f"  - {err}")
    else:
        logger.info("All health checks passed")
```

- [ ] **Step 5: Verify endpoints**

Run:
```
curl http://localhost:8000/status/nonexistent  # → 404
curl http://localhost:8000/health               # → health JSON
```

---

### Task 6: Frontend (single HTML page + static files mount)

**Files:**
- Create: `static/index.html`
- Modify: `server.py` — add index route and static mount at the END

- [ ] **Step 1: Write the complete index.html**

```html
<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Anime4K Upscaler</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0f0f14;
    color: #e0e0e0;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 40px 20px;
  }
  h1 {
    font-size: 28px;
    font-weight: 600;
    margin-bottom: 8px;
    letter-spacing: -0.5px;
  }
  .subtitle { color: #888; font-size: 14px; margin-bottom: 32px; }

  .drop-zone {
    width: 100%;
    max-width: 600px;
    border: 2px dashed #333;
    border-radius: 16px;
    padding: 60px 30px;
    text-align: center;
    transition: border-color 0.2s, background 0.2s;
    cursor: pointer;
  }
  .drop-zone.dragover { border-color: #7c5cfc; background: rgba(124,92,252,0.06); }
  .drop-zone.has-file { border-color: #7c5cfc; border-style: solid; background: rgba(124,92,252,0.04); }
  .drop-zone h2 { font-size: 18px; font-weight: 500; margin-bottom: 8px; }
  .drop-zone p { color: #666; font-size: 13px; }
  .drop-zone input { display: none; }

  .mode-selector {
    display: flex;
    gap: 12px;
    justify-content: center;
    margin: 24px 0;
  }
  .mode-btn {
    padding: 10px 28px;
    border: 1px solid #333;
    border-radius: 10px;
    background: #1a1a24;
    color: #ccc;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
    transition: all 0.15s;
  }
  .mode-btn:hover { border-color: #555; }
  .mode-btn.active { border-color: #7c5cfc; background: rgba(124,92,252,0.12); color: #fff; }

  .progress-container {
    max-width: 600px;
    width: 100%;
    margin-top: 20px;
    display: none;
  }
  .progress-bar-bg {
    width: 100%;
    height: 6px;
    background: #1a1a24;
    border-radius: 3px;
    overflow: hidden;
  }
  .progress-bar-fill {
    height: 100%;
    background: #7c5cfc;
    border-radius: 3px;
    transition: width 0.4s ease;
    width: 0%;
  }
  .progress-text { margin-top: 8px; font-size: 13px; color: #888; }

  .result { margin-top: 24px; display: none; text-align: center; }
  .result a {
    display: inline-block;
    padding: 12px 32px;
    background: #7c5cfc;
    color: #fff;
    text-decoration: none;
    border-radius: 10px;
    font-weight: 500;
    font-size: 14px;
    transition: background 0.15s;
  }
  .result a:hover { background: #6a4be8; }

  .error-box {
    margin-top: 24px;
    padding: 14px 20px;
    background: rgba(220,50,50,0.12);
    border: 1px solid rgba(220,50,50,0.3);
    border-radius: 10px;
    color: #f06060;
    font-size: 13px;
    max-width: 600px;
    display: none;
  }

  .file-info {
    margin-top: 12px;
    font-size: 13px;
    color: #aaa;
    display: none;
  }
</style>
</head>
<body>

<h1>Anime4K Upscaler</h1>
<p class="subtitle">Upscale anime videos — HD 1080p or UHD 4K</p>

<div class="drop-zone" id="dropZone">
  <h2>Drop a video here</h2>
  <p>or click to browse — max 5 GB</p>
  <input type="file" id="fileInput" accept="video/*">
  <div class="file-info" id="fileInfo"></div>
</div>

<div class="mode-selector">
  <button class="mode-btn active" data-mode="hd">HD (1080p)</button>
  <button class="mode-btn" data-mode="4k">UHD 4K</button>
</div>

<div class="progress-container" id="progressContainer">
  <div class="progress-bar-bg">
    <div class="progress-bar-fill" id="progressFill"></div>
  </div>
  <div class="progress-text" id="progressText">Starting...</div>
</div>

<div class="error-box" id="errorBox"></div>

<div class="result" id="result">
  <a href="#" id="downloadLink" download>Download Upscaled Video</a>
</div>

<script>
  const dropZone = document.getElementById('dropZone');
  const fileInput = document.getElementById('fileInput');
  const fileInfo = document.getElementById('fileInfo');
  const progressContainer = document.getElementById('progressContainer');
  const progressFill = document.getElementById('progressFill');
  const progressText = document.getElementById('progressText');
  const errorBox = document.getElementById('errorBox');
  const result = document.getElementById('result');
  const downloadLink = document.getElementById('downloadLink');
  const modeBtns = document.querySelectorAll('.mode-btn');

  let selectedMode = 'hd';
  let selectedFile = null;
  let pollTimer = null;

  modeBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      modeBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedMode = btn.dataset.mode;
    });
  });

  dropZone.addEventListener('click', () => fileInput.click());

  ['dragenter', 'dragover'].forEach(evt => {
    dropZone.addEventListener(evt, e => { e.preventDefault(); dropZone.classList.add('dragover'); });
  });
  ['dragleave', 'drop'].forEach(evt => {
    dropZone.addEventListener(evt, e => { e.preventDefault(); dropZone.classList.remove('dragover'); });
  });

  dropZone.addEventListener('drop', e => {
    const files = e.dataTransfer.files;
    if (files.length > 0) setFile(files[0]);
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files.length > 0) setFile(fileInput.files[0]);
  });

  function setFile(file) {
    if (!file.type.startsWith('video/')) {
      showError('Please select a video file.');
      return;
    }
    selectedFile = file;
    fileInfo.textContent = `${file.name} (${formatSize(file.size)})`;
    fileInfo.style.display = 'block';
    dropZone.classList.add('has-file');
    dropZone.querySelector('h2').textContent = 'Click to process';
    dropZone.querySelector('p').textContent = 'or drop another video to replace';
    hideError();
    hideResult();
    uploadFile();
  }

  async function uploadFile() {
    progressContainer.style.display = 'block';
    progressFill.style.width = '0%';
    progressText.textContent = 'Uploading...';

    const form = new FormData();
    form.append('file', selectedFile);
    form.append('mode', selectedMode);

    try {
      const res = await fetch('/upload', { method: 'POST', body: form });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Upload failed (${res.status})`);
      }
      const data = await res.json();
      pollStatus(data.task_id);
    } catch (err) {
      showError(err.message);
      progressContainer.style.display = 'none';
    }
  }

  function pollStatus(taskId) {
    progressText.textContent = 'Processing on GPU...';
    pollTimer = setInterval(async () => {
      try {
        const res = await fetch(`/status/${taskId}`);
        if (!res.ok) { clearInterval(pollTimer); showError('Status check failed'); return; }
        const job = await res.json();

        progressFill.style.width = job.progress + '%';
        if (job.status === 'uploaded') progressText.textContent = 'Queued...';
        else if (job.status === 'processing') progressText.textContent = `Processing on GPU... ${job.progress}%`;
        else if (job.status === 'done') {
          clearInterval(pollTimer);
          progressText.textContent = 'Done!';
          progressFill.style.width = '100%';
          downloadLink.href = `/download/${taskId}`;
          downloadLink.download = `upscaled_${selectedFile.name}`;
          result.style.display = 'block';
        } else if (job.status === 'failed') {
          clearInterval(pollTimer);
          progressContainer.style.display = 'none';
          showError(job.error || 'Processing failed');
        }
      } catch {
        clearInterval(pollTimer);
        showError('Connection lost');
      }
    }, 1500);
  }

  function showError(msg) {
    errorBox.textContent = msg;
    errorBox.style.display = 'block';
  }
  function hideError() { errorBox.style.display = 'none'; }
  function hideResult() { result.style.display = 'none'; }
  function formatSize(bytes) {
    if (bytes > 1e9) return (bytes / 1e9).toFixed(1) + ' GB';
    if (bytes > 1e6) return (bytes / 1e6).toFixed(1) + ' MB';
    return (bytes / 1e3).toFixed(1) + ' KB';
  }
</script>
</body>
</html>
```

- [ ] **Step 2: Add index route and static mount to server.py (at the very end)**

Append to `server.py`:

```python
@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path("static") / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory="static"), name="static")
```

- [ ] **Step 3: Verify frontend loads**

Run: `uvicorn server:app --port 8000`
Open `http://localhost:8000` in browser.
Check: dark theme, drop zone visible, mode buttons toggle properly.

---

### Task 7: install.sh script

**Files:**
- Create: `install.sh`

- [ ] **Step 1: Write install.sh**

```bash
#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[+]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[x]${NC} $1"; exit 1; }

echo "============================================"
echo " Anime4K Web Upscaler — Installation"
echo "============================================"
echo ""

# Check OS
if [ "$(uname -s)" != "Linux" ]; then
    err "This installer is for Linux only."
fi

# Check CUDA
log "Checking for NVIDIA GPU + CUDA..."
if ! command -v nvidia-smi &>/dev/null; then
    err "nvidia-smi not found. Install NVIDIA drivers + CUDA toolkit first."
fi
nvidia-smi --query-gpu=name --format=csv,noheader | head -1

# Check FFmpeg
log "Checking FFmpeg..."
if ! command -v ffmpeg &>/dev/null; then
    warn "ffmpeg not found. Installing..."
    if command -v apt &>/dev/null; then
        sudo apt update && sudo apt install -y ffmpeg libavcodec-dev libavformat-dev libavutil-dev libswscale-dev
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y ffmpeg ffmpeg-devel
    else
        err "Cannot auto-install ffmpeg. Please install it manually."
    fi
else
    ffmpeg -version | head -1
fi

# Check for build tools
log "Checking build tools..."
for tool in cmake g++ make; do
    if ! command -v $tool &>/dev/null; then
        warn "$tool not found. Installing..."
        if command -v apt &>/dev/null; then
            sudo apt install -y build-essential cmake
        elif command -v dnf &>/dev/null; then
            sudo dnf groupinstall -y "Development Tools" && sudo dnf install -y cmake
        else
            err "Need $tool. Please install it manually."
        fi
        break
    fi
done

# Check if ac_cli already exists
ANIME4KCPP_DIR="$HOME/anime4kcpp"
if [ -x "$ANIME4KCPP_DIR/build/bin/ac_cli" ]; then
    log "ac_cli already installed at $ANIME4KCPP_DIR/build/bin/ac_cli"
    read -rp "Recompile? [y/N] " recompile
    if [ "$recompile" != "y" ] && [ "$recompile" != "Y" ]; then
        log "Skipping Anime4KCPP build."
        log "Installation complete!"
        echo ""
        echo "Add to PATH: export PATH=\"$ANIME4KCPP_DIR/build/bin:\$PATH\""
        echo "Then: cd $(pwd) && source venv/bin/activate && uvicorn server:app --host 0.0.0.0 --port 8000"
        exit 0
    fi
fi

# Build Anime4KCPP
log "Cloning and building Anime4KCPP..."
mkdir -p "$ANIME4KCPP_DIR"
cd "$ANIME4KCPP_DIR"

if [ -f "CMakeLists.txt" ]; then
    log "Anime4KCPP source already present, pulling latest..."
    git pull || true
else
    git clone https://github.com/TianZerL/Anime4KCPP.git .
fi

mkdir -p build && cd build

log "Running CMake (with CUDA + video support)..."
cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DAC_CORE_WITH_CUDA=ON \
    -DAC_BUILD_CLI=ON \
    -DAC_BUILD_VIDEO=ON \
    -DAC_BUILD_GUI=OFF \
    -DCMAKE_CUDA_ARCHITECTURES="75;80;86;89;90"

log "Building (this will take a few minutes)..."
cmake --build . --config Release -j"$(nproc)"

if [ ! -x "bin/ac_cli" ]; then
    err "Build failed: ac_cli not found in build/bin/"
fi

log "ac_cli built successfully."
"$(pwd)/bin/ac_cli" -v || warn "ac_cli -v returned non-zero, but binary exists"

echo ""
log "============================================"
log " Installation complete!"
log "============================================"
echo ""
echo "Add this to your PATH or use full path:"
echo "  export PATH=\"$ANIME4KCPP_DIR/build/bin:\$PATH\""
echo ""
echo "Then start the server:"
echo "  cd $(pwd)/anime4k-web"
echo "  source venv/bin/activate"
echo "  uvicorn server:app --host 0.0.0.0 --port 8000"
```

- [ ] **Step 2: Make install.sh executable**

Run: `chmod +x install.sh`
