import subprocess
import shutil
import os
import logging
import asyncio
import uuid
import time
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("anime4k-web")

# ---------------------------------------------------------------------------
# Dependency / health checks
# ---------------------------------------------------------------------------

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
            capture_output=True, text=True, timeout=10,
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
            target = d if os.path.exists(d) else "."
            stat = os.statvfs(target)
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


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Anime4K Web Upscaler")

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

MAX_FILE_SIZE = 5 * 1024 * 1024 * 1024  # 5 GB

jobs: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Video processing
# ---------------------------------------------------------------------------

async def run(cmd: list[str], timeout: float | None = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")[:1000]


async def process_video(task_id: str, input_path: str, mode: str, model: str):
    try:
        jobs[task_id]["status"] = "processing"
        jobs[task_id]["progress"] = 5

        factor = 2 if mode == "hd" else 4
        audio_track = str(OUTPUT_DIR / f"{task_id}_audio.aac")
        frames_dir = Path(OUTPUT_DIR) / f"{task_id}_frames"
        upscaled_dir = Path(OUTPUT_DIR) / f"{task_id}_frames_up"
        final_output = str(OUTPUT_DIR / f"{task_id}_upscaled.mp4")

        # --- Step 1: Extract audio + get video info ---
        jobs[task_id]["progress"] = 5
        logger.info("Extracting audio...")

        rc, _, _ = await run([
            "ffmpeg", "-y", "-i", input_path, "-vn", "-acodec", "copy", audio_track,
        ])
        has_audio = rc == 0 and Path(audio_track).exists() and Path(audio_track).stat().st_size > 0

        logger.info("Getting video info...")
        _, fps_str, _ = await run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1", input_path,
        ])

        # Parse FPS (e.g. "24000/1001" or "30/1")
        fps = 24.0
        try:
            num, den = fps_str.strip().split("/")
            fps = float(num) / float(den) if float(den) != 0 else 24.0
        except Exception:
            logger.warning(f"Cannot parse FPS from '{fps_str.strip()}', defaulting to 24")

        # --- Step 2: Extract frames ---
        jobs[task_id]["progress"] = 10
        logger.info("Extracting frames...")
        frames_dir.mkdir(exist_ok=True)

        rc, _, err = await run([
            "ffmpeg", "-y", "-i", input_path,
            str(frames_dir / "%06d.png"),
        ])
        if rc != 0:
            raise RuntimeError(f"Frame extraction failed: {err}")

        pngs = sorted(frames_dir.glob("*.png"))
        total_frames = len(pngs)
        if total_frames == 0:
            raise RuntimeError("No frames extracted from video")

        logger.info(f"Extracted {total_frames} frames at {fps:.2f} fps")

        # --- Step 3: Upscale frames with ac_cli ---
        jobs[task_id]["progress"] = 20
        upscaled_dir.mkdir(exist_ok=True)

        # Process frames in parallel (8 concurrent ac_cli instances)
        sem = asyncio.Semaphore(8)

        async def upscale_frame(png_path: Path):
            async with sem:
                out_path = str(upscaled_dir / png_path.name)
                rc, _, err = await run([
                    "ac_cli",
                    "-i", str(png_path),
                    "-o", out_path,
                    "-p", "cuda",
                    "-d", "0",
                    "-f", str(factor),
                    "-m", model,
                ], timeout=120)
                if rc != 0:
                    logger.warning(f"Frame {png_path.name} failed: {err}")

        tasks = [upscale_frame(p) for p in pngs]

        for i, batch_start in enumerate(range(0, len(tasks), 16)):
            batch = tasks[batch_start:batch_start + 16]
            await asyncio.gather(*batch)
            done = min(batch_start + 16, len(tasks))
            pct = 20 + int(55 * (done / total_frames))
            jobs[task_id]["progress"] = pct
            logger.info(f"Upscaled {done}/{total_frames} frames")

        # --- Step 4: Re-encode frames to video (NVENC hardware) ---
        jobs[task_id]["progress"] = 80

        upscaled_pngs = sorted(upscaled_dir.glob("*.png"))
        if not upscaled_pngs:
            raise RuntimeError("No upscaled frames produced")

        encode_cmd = [
            "ffmpeg", "-y",
            "-framerate", str(fps),
            "-i", str(upscaled_dir / "%06d.png"),
        ]

        if has_audio:
            encode_cmd += ["-i", audio_track]
            encode_cmd += [
                "-c:v", "h264_nvenc",
                "-preset", "p1",
                "-tune", "hq",
                "-rc", "vbr",
                "-cq", "19",
                "-b:v", "0",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-shortest",
                final_output,
            ]
        else:
            encode_cmd += [
                "-c:v", "h264_nvenc",
                "-preset", "p1",
                "-tune", "hq",
                "-rc", "vbr",
                "-cq", "19",
                "-b:v", "0",
                "-pix_fmt", "yuv420p",
                "-map", "0:v:0",
                final_output,
            ]

        jobs[task_id]["progress"] = 85
        logger.info(f"Encoding final video ({len(upscaled_pngs)} frames, {fps:.2f} fps)...")

        rc, _, err = await run(encode_cmd, timeout=3600)
        if rc != 0:
            raise RuntimeError(f"Final encoding failed: {err}")

        # --- Cleanup ---
        jobs[task_id]["progress"] = 95

        shutil.rmtree(str(frames_dir), ignore_errors=True)
        shutil.rmtree(str(upscaled_dir), ignore_errors=True)
        Path(audio_track).unlink(missing_ok=True)

        jobs[task_id]["status"] = "done"
        jobs[task_id]["progress"] = 100
        jobs[task_id]["output"] = f"{task_id}_upscaled.mp4"
        logger.info(f"Task {task_id}: done -> {final_output}")

    except Exception as e:
        logger.error(f"Task {task_id} unexpected error: {e}")
        jobs[task_id]["status"] = "failed"
        jobs[task_id]["error"] = str(e)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

async def cleanup_old_files():
    while True:
        await asyncio.sleep(3600)
        now = time.time()
        for d, max_age, label in [
            (UPLOAD_DIR, 3600, "uploads"),
            (OUTPUT_DIR, 86400, "outputs"),
        ]:
            for f in d.iterdir():
                if f.is_file() and (now - f.stat().st_mtime) > max_age:
                    f.unlink(missing_ok=True)
                    logger.info(f"Cleanup {label}: removed {f.name}")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return run_health_checks()


@app.post("/upload")
async def upload_video(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    mode: str = Form("hd"),
    model: str = Form("arnet-b64-ls"),
):
    if mode not in ("hd", "4k"):
        raise HTTPException(400, "Mode must be 'hd' or '4k'")

    valid_models = {"acnet-gan", "arnet-b64-ls", "artcnn-c4f32"}
    if model not in valid_models:
        raise HTTPException(400, f"Model must be one of: {', '.join(valid_models)}")

    task_id = uuid.uuid4().hex[:12]
    ext = Path(file.filename).suffix or ".mp4"
    safe_name = f"{task_id}{ext}"
    input_path = UPLOAD_DIR / safe_name

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
        "model": model,
        "filename": file.filename,
        "output": None,
        "error": None,
    }

    background_tasks.add_task(process_video, task_id, str(input_path), mode, model)
    return {"task_id": task_id}


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


@app.get("/download/{task_id}")
async def download_video(task_id: str):
    job = jobs.get(task_id)
    if job is None:
        raise HTTPException(404, "Task not found")

    if job["status"] == "failed":
        raise HTTPException(500, job.get("error", "Processing failed"))
    if job["status"] != "done":
        raise HTTPException(425, f"Not ready (status: {job['status']})")

    output_path = OUTPUT_DIR / job["output"]
    if not output_path.exists():
        raise HTTPException(404, "Output file missing — may have been cleaned up")

    return FileResponse(
        str(output_path),
        media_type="video/mp4",
        filename=job.get("filename", "upscaled.mp4"),
    )


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path("static") / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


app.mount("/static", StaticFiles(directory="static"), name="static")
