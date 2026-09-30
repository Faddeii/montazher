"""Веб-сервис: загрузка видео → расшифровка → анализ → ручная проверка → рендер и экспорт."""
import json
import re
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import analyze, capcut, export, media
from .config import JOBS_DIR, WEB_DIR, has_claude_key

app = FastAPI(title="Монтажёр")

# Распознавание и рендер нагружают видеокарту — выполняем по одной задаче каждого вида
pipeline_pool = ThreadPoolExecutor(max_workers=1)
render_pool = ThreadPoolExecutor(max_workers=1)
_lock = threading.Lock()

OUTPUTS = {"mp4": "result.mp4", "xml": "premiere_davinci.xml", "edl": "edit.edl"}


# ---------- хранилище задач ----------

def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404, "Нет такой задачи")
    d = JOBS_DIR / job_id
    if not (d / "job.json").exists():
        raise HTTPException(404, "Нет такой задачи")
    return d


# В Windows файл нельзя заменить, пока его кто-то читает (страница опрашивает статус каждую секунду,
# антивирус проверяет новые файлы) — тогда «Отказано в доступе». Поэтому чтение и запись повторяем.
RETRIES, RETRY_DELAY = 100, 0.05


def read_json(path: Path, default=None):
    for attempt in range(RETRIES):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (PermissionError, json.JSONDecodeError):
            if attempt == RETRIES - 1:
                raise
            time.sleep(RETRY_DELAY)


def write_json(path: Path, data) -> None:
    tmp = path.with_name(f"{path.stem}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for attempt in range(RETRIES):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == RETRIES - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(RETRY_DELAY)


def update_job(job_id: str, **fields) -> dict:
    with _lock:
        path = JOBS_DIR / job_id / "job.json"
        job = read_json(path)
        if "stage" in fields and fields["stage"] != job.get("stage"):
            job["stage_started"] = time.time()  # чтобы страница показывала, сколько идёт этап
        for k, v in fields.items():
            if isinstance(v, dict) and isinstance(job.get(k), dict):
                job[k].update(v)
            else:
                job[k] = v
        write_json(path, job)
        return job


def throttled(job_id: str, stage: str, key: str = "progress"):
    last = [0.0]

    def cb(p: float):
        now = time.monotonic()
        if now - last[0] > 0.5 or p >= 1:
            last[0] = now
            if key == "progress":
                update_job(job_id, stage=stage, progress=round(p, 3))
            else:
                update_job(job_id, render={"progress": round(p, 3)})
    return cb


# ---------- конвейер обработки ----------

def run_pipeline(job_id: str) -> None:
    d = JOBS_DIR / job_id
    try:
        job = read_json(d / "job.json")
        src = d / job["source"]
        update_job(job_id, status="processing", stage="probe", progress=0)
        info = media.probe(src)
        update_job(job_id, media=info)

        update_job(job_id, stage="audio", progress=0)
        media.extract_audio(src, d / "audio.wav")

        if not media.browser_playable(src, info):
            update_job(job_id, stage="proxy", progress=0)
            media.make_proxy(src, d / "preview.mp4", info, throttled(job_id, "proxy"))
            update_job(job_id, preview="preview.mp4")

        run_transcribe(job_id)
        run_analyze(job_id)
        update_job(job_id, status="ready", stage="done", progress=1)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        update_job(job_id, status="error", error=str(e))


def run_transcribe(job_id: str) -> None:
    from .model_store import ensure_model, model_ready
    from .transcribe import model_device, transcribe  # тяжёлый импорт (CUDA) — только когда нужен

    d = JOBS_DIR / job_id
    if not model_ready():  # первый запуск: модель ≈1,6 ГБ, показываем прогресс скачивания
        update_job(job_id, stage="model", progress=0)
        ensure_model(throttled(job_id, "model"))
    update_job(job_id, stage="transcribe", progress=0)
    update_job(job_id, device=model_device())
    audio = media.load_wav_16k(d / "audio.wav")
    write_json(d / "transcript_raw.json", transcribe(audio, throttled(job_id, "transcribe")))


def run_analyze(job_id: str) -> None:
    from . import acoustic
    from .transcribe import classify_clip, clip_words

    d = JOBS_DIR / job_id
    if not (d / "transcript_raw.json").exists():  # задачи, обработанные старой версией
        shutil.copy2(d / "transcript.json", d / "transcript_raw.json")
    raw = read_json(d / "transcript_raw.json")
    words = [w for w in raw["words"] if not w.get("synthetic") and not w.get("recovered")]

    # Мычание, которое Whisper не записал в текст, ищем по звуку
    update_job(job_id, stage="acoustic", progress=0)
    audio = media.load_wav_16k(d / "audio.wav")
    words = acoustic.refine(words, audio, classify_clip, throttled(job_id, "acoustic"))
    words = acoustic.split_long_words(words, audio, clip_words, throttled(job_id, "acoustic"))
    write_json(d / "transcript.json", {**raw, "words": words})

    update_job(job_id, stage="analyze", progress=0)
    result = analyze.analyze(words, throttled(job_id, "analyze"))
    write_json(d / "analysis.json", result)
    (d / "edits.json").unlink(missing_ok=True)  # новая разметка — старые ручные правки не подходят


def run_render(job_id: str, segments: list[list[float]]) -> None:
    d = JOBS_DIR / job_id
    try:
        job = read_json(d / "job.json")
        info, src = job["media"], d / job["source"]
        segs = media.snap_segments(segments, info)
        if not segs:
            raise RuntimeError("После монтажа ничего не осталось")
        for key in OUTPUTS.values():
            (d / key).unlink(missing_ok=True)
        (d / OUTPUTS["xml"]).write_text(export.fcp_xml(src, job["name"], info, segs), encoding="utf-8")
        (d / OUTPUTS["edl"]).write_text(export.edl(job["name"], info, segs), encoding="utf-8")
        update_job(job_id, render={"status": "rendering", "progress": 0, "error": None, "files": ["xml", "edl"]})
        media.render(src, d / OUTPUTS["mp4"], segs, info, d, throttled(job_id, "", key="render"))
        update_job(job_id, render={"status": "done", "progress": 1, "files": ["mp4", "xml", "edl"],
                                   "duration": sum(e - s for s, e in segs)})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        update_job(job_id, render={"status": "error", "error": str(e)})


@app.on_event("startup")
def resume_interrupted():
    """Если сервер перезапустили посреди обработки — доделываем незавершённые задачи."""
    for p in JOBS_DIR.glob("*/job.json"):
        job = read_json(p)
        if job["status"] in ("queued", "processing"):
            pipeline_pool.submit(run_pipeline, job["id"])
        if job["render"].get("status") == "rendering":
            update_job(job["id"], render={"status": "error", "error": "Рендер прерван перезапуском сервера"})


# ---------- API ----------

@app.get("/api/status")
def status():
    return {"claude": has_claude_key()}


@app.get("/api/jobs")
def list_jobs():
    jobs = [read_json(p) for p in JOBS_DIR.glob("*/job.json")]
    return sorted((j for j in jobs if j), key=lambda j: j["created"], reverse=True)


@app.post("/api/jobs")
async def create_job(request: Request, filename: str):
    name = Path(filename).name or "video.mp4"
    ext = Path(name).suffix.lower() or ".mp4"
    job_id = uuid.uuid4().hex[:12]
    d = JOBS_DIR / job_id
    d.mkdir(parents=True)
    source = f"source{ext}"
    try:
        with open(d / source, "wb") as f:
            async for chunk in request.stream():
                f.write(chunk)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    write_json(d / "job.json", {"id": job_id, "name": name, "source": source, "created": time.time(),
                                "status": "queued", "stage": "queued", "progress": 0, "error": None,
                                "preview": source, "media": None, "render": {"status": "idle"}})
    pipeline_pool.submit(run_pipeline, job_id)
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return read_json(job_dir(job_id) / "job.json")


@app.get("/api/jobs/{job_id}/data")
def get_data(job_id: str):
    d = job_dir(job_id)
    return {
        "job": read_json(d / "job.json"),
        "transcript": read_json(d / "transcript.json"),
        "analysis": read_json(d / "analysis.json"),
        "edits": read_json(d / "edits.json"),
    }


@app.put("/api/jobs/{job_id}/edits")
async def save_edits(job_id: str, request: Request):
    write_json(job_dir(job_id) / "edits.json", await request.json())
    return {"ok": True}


@app.post("/api/jobs/{job_id}/analyze")
def reanalyze(job_id: str):
    d = job_dir(job_id)
    if not (d / "transcript.json").exists():
        raise HTTPException(409, "Расшифровка ещё не готова")

    def task():
        try:
            update_job(job_id, status="processing")
            run_analyze(job_id)
            update_job(job_id, status="ready", stage="done", progress=1)
        except Exception as e:  # noqa: BLE001
            update_job(job_id, status="error", error=str(e))
    pipeline_pool.submit(task)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/media")
def get_media(job_id: str):
    d = job_dir(job_id)
    return FileResponse(d / read_json(d / "job.json")["preview"])


class RenderRequest(BaseModel):
    segments: list[list[float]]


@app.post("/api/jobs/{job_id}/render")
def start_render(job_id: str, body: RenderRequest):
    d = job_dir(job_id)
    if read_json(d / "job.json")["render"].get("status") == "rendering":
        raise HTTPException(409, "Рендер уже идёт")
    update_job(job_id, render={"status": "rendering", "progress": 0, "error": None, "files": []})
    render_pool.submit(run_render, job_id, body.segments)
    return {"ok": True}


@app.get("/api/capcut")
def capcut_status():
    d = capcut.drafts_dir()
    return {"available": d is not None}


@app.post("/api/jobs/{job_id}/capcut")
def to_capcut(job_id: str, body: RenderRequest):
    d = job_dir(job_id)
    job = read_json(d / "job.json")
    segs = media.snap_segments(body.segments, job["media"])
    if not segs:
        raise HTTPException(400, "После монтажа ничего не осталось")
    try:
        folder = capcut.build_draft(d / job["source"], job["name"], job["media"], segs)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    return {"name": folder.name, "clips": len(segs)}


@app.get("/api/jobs/{job_id}/download/{kind}")
def download(job_id: str, kind: str):
    d = job_dir(job_id)
    if kind not in OUTPUTS or not (d / OUTPUTS[kind]).exists():
        raise HTTPException(404, "Файл ещё не готов")
    stem = Path(read_json(d / "job.json")["name"]).stem
    ext = {"mp4": ".mp4", "xml": ".xml", "edl": ".edl"}[kind]
    return FileResponse(d / OUTPUTS[kind], filename=f"{stem} (монтаж){ext}")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    d = job_dir(job_id)
    if read_json(d / "job.json")["status"] in ("queued", "processing"):
        raise HTTPException(409, "Файл ещё обрабатывается")
    shutil.rmtree(d)
    return {"ok": True}


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
