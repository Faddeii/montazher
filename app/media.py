"""Обёртки над ffmpeg/ffprobe: анализ файла, извлечение звука, превью и финальный рендер."""
import json
import subprocess
import wave
from fractions import Fraction
from pathlib import Path
from typing import Callable

import numpy as np

from .config import FFMPEG, FFPROBE

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BROWSER_VIDEO = {"h264", "vp8", "vp9", "av1"}
BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis"}
BROWSER_CONTAINERS = {".mp4", ".m4v", ".mov", ".webm"}


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NO_WINDOW)


def probe(path: Path) -> dict:
    r = _run([FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe не смог прочитать файл: {r.stderr.strip()[-500:]}")
    info = json.loads(r.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if a is None:
        raise RuntimeError("В файле нет звуковой дорожки — нечего анализировать")
    fps = Fraction(v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1") if v else Fraction(30)
    if fps <= 0:
        fps = Fraction(v.get("r_frame_rate", "30/1")) if v else Fraction(30)
    return {
        "duration": float(info["format"].get("duration") or a.get("duration") or 0),
        "has_video": v is not None,
        "video_codec": v["codec_name"] if v else None,
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "fps_num": fps.numerator,
        "fps_den": fps.denominator,
        "fps": float(fps),
        "audio_codec": a["codec_name"],
        "sample_rate": int(a.get("sample_rate", 48000)),
        "channels": int(a.get("channels", 2)),
    }


def browser_playable(path: Path, info: dict) -> bool:
    return (path.suffix.lower() in BROWSER_CONTAINERS
            and (not info["has_video"] or info["video_codec"] in BROWSER_VIDEO)
            and info["audio_codec"] in BROWSER_AUDIO)


def extract_audio(src: Path, dst: Path) -> None:
    r = _run([FFMPEG, "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
              "-c:a", "pcm_s16le", str(dst)])
    if r.returncode != 0:
        raise RuntimeError(f"Не удалось извлечь звук: {r.stderr.strip()[-500:]}")


def load_wav_16k(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0


def _ffmpeg_with_progress(args: list[str], total: float, on_progress: Callable[[float], None]) -> tuple[int, str]:
    proc = subprocess.Popen([FFMPEG, "-y", "-v", "error", "-nostats", "-progress", "pipe:1", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and total > 0:
            try:
                on_progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / total))
            except ValueError:
                pass
    err = proc.stderr.read()
    return proc.wait(), err


def _video_encoders(nvenc: bool) -> list[str]:
    if nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]


def make_proxy(src: Path, dst: Path, info: dict, on_progress: Callable[[float], None]) -> None:
    """Лёгкая копия для просмотра в браузере, если исходник браузер не воспроизводит."""
    scale = ["-vf", "scale=-2:'min(720,ih)'"] if info["has_video"] else ["-vn"]
    for nvenc in (True, False):
        code, err = _ffmpeg_with_progress(
            ["-i", str(src), *scale, *(_video_encoders(nvenc) if info["has_video"] else []),
             "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)],
            info["duration"], on_progress)
        if code == 0:
            return
    raise RuntimeError(f"Не удалось сделать превью: {err.strip()[-500:]}")


def snap_segments(segments: list[list[float]], info: dict) -> list[tuple[float, float]]:
    """Выравнивает границы по кадрам и отбрасывает пустые куски."""
    fps = info["fps"] if info["has_video"] else 100.0
    out = []
    for s, e in segments:
        s = round(max(0.0, s) * fps) / fps
        e = round(min(info["duration"], e) * fps) / fps
        if e - s >= 1.5 / fps:
            out.append((s, e))
    return out


def render(src: Path, dst: Path, segments: list[tuple[float, float]], info: dict,
           work_dir: Path, on_progress: Callable[[float], None]) -> None:
    """Склеивает оставленные куски в один файл за один проход.

    select/aselect выбирают нужные кадры, а новые таймкоды видео считаются как сумма
    уже пройденных кусков — это корректно работает и с переменной частотой кадров (запись с телефона).
    """
    keep = "+".join(f"between(t,{s:.4f},{e:.4f})" for s, e in segments)
    remap = "+".join(f"clip(T-{s:.4f},0,{e - s:.4f})" for s, e in segments)
    total = sum(e - s for s, e in segments)
    graph = [f"[0:a]asetnsamples=n=256:p=0,aselect='{keep}',asetpts=N/SR/TB[a]"]
    maps = ["-map", "[a]"]
    if info["has_video"]:
        graph.insert(0, f"[0:v]select='{keep}',setpts='({remap})/TB'[v]")
        maps = ["-map", "[v]", *maps]
    script = work_dir / "render_filter.txt"
    script.write_text(";\n".join(graph), encoding="utf-8")

    err = ""
    for nvenc in (True, False):
        venc = [*_video_encoders(nvenc), "-fps_mode", "vfr"] if info["has_video"] else []
        code, err = _ffmpeg_with_progress(
            ["-i", str(src), "-/filter_complex", str(script), *maps, *venc,
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)],
            total, on_progress)
        if code == 0:
            return
    raise RuntimeError(f"Ошибка рендера: {err.strip()[-800:]}")
