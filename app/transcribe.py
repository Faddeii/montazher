"""Распознавание речи (faster-whisper) с таймкодами слов."""
import os
import re
import sys
import threading
from pathlib import Path
from typing import Callable

import numpy as np

from .config import LANGUAGE, WHISPER_DEVICE, WHISPER_MODEL


def _add_cuda_dlls() -> None:
    """cuBLAS/cuDNN ставятся pip-пакетами nvidia-*; на Windows их папки нужно добавить в поиск DLL."""
    if sys.platform != "win32":
        return
    nvidia = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    for bin_dir in nvidia.glob("*/bin"):
        os.add_dll_directory(str(bin_dir))
        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


_add_cuda_dlls()
from faster_whisper import WhisperModel  # noqa: E402

# Подсказка заставляет Whisper записывать мычание, а не выкидывать его из текста
INITIAL_PROMPT = "Эээ, ну, мм... Так, э-э, значит, я, эм, хотел сказать, что-о... ну, вот."

# Типичные «галлюцинации» Whisper на тишине в русском языке
HALLUCINATIONS = re.compile(
    r"субтитр|dimatorzok|редактор\s+субтитров|продолжение следует|подписывайтесь на канал|"
    r"корректор\s+[а-я]\.|amara\.org", re.IGNORECASE)

_model = None
_model_lock = threading.Lock()


def get_model() -> WhisperModel:
    global _model
    with _model_lock:
        if _model is None:
            device = WHISPER_DEVICE
            if device == "auto":
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            try:
                _model = WhisperModel(WHISPER_MODEL, device=device,
                                      compute_type="float16" if device == "cuda" else "int8")
            except Exception as e:  # нет CUDA — работаем на процессоре, медленнее
                print(f"[whisper] GPU недоступен ({e}), переключаюсь на CPU")
                _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        return _model


def transcribe(audio: np.ndarray, on_progress: Callable[[float], None]) -> dict:
    duration = len(audio) / 16000
    segments, _ = get_model().transcribe(
        audio,
        language=LANGUAGE,
        word_timestamps=True,
        beam_size=5,
        initial_prompt=INITIAL_PROMPT,
        condition_on_previous_text=False,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 700, "speech_pad_ms": 400},
    )
    words = []
    for seg in segments:
        on_progress(min(1.0, seg.end / duration) if duration else 1.0)
        if HALLUCINATIONS.search(seg.text):
            continue
        for w in seg.words or []:
            text = w.word.strip()
            if text:
                words.append({"w": text, "s": round(w.start, 3), "e": round(w.end, 3),
                              "p": round(w.probability, 3)})
    for i, w in enumerate(words):
        w["i"] = i
    return {"duration": duration, "words": words}


def classify_clip(clip: np.ndarray) -> tuple[str, float]:
    """Что слышно в коротком куске и насколько Whisper уверен (средний log-prob, ближе к 0 — увереннее).
    Тишина по краям помогает Whisper; temperature=0 — чтобы ответ не менялся от запуска к запуску."""
    pad = np.zeros(8000, dtype=np.float32)
    segments, _ = get_model().transcribe(np.concatenate([pad, clip, pad]), language=LANGUAGE,
                                         initial_prompt="Эээ, ммм, ну...", beam_size=5, temperature=0.0,
                                         condition_on_previous_text=False, vad_filter=False)
    segments = list(segments)
    if not segments:
        return "", -10.0
    return " ".join(s.text for s in segments).strip(), min(s.avg_logprob for s in segments)


def clip_words(clip: np.ndarray) -> list[tuple[str, float, float]]:
    """Слова короткого куска с таймкодами относительно его начала."""
    pad = np.zeros(8000, dtype=np.float32)
    segments, _ = get_model().transcribe(np.concatenate([pad, clip, pad]), language=LANGUAGE,
                                         initial_prompt="Эээ, ммм, ну...", beam_size=5, temperature=0.0,
                                         condition_on_previous_text=False, vad_filter=False, word_timestamps=True)
    out = []
    for seg in segments:
        if HALLUCINATIONS.search(seg.text):
            return []
        for w in seg.words or []:
            out.append((w.word.strip(), max(0.0, w.start - 0.5), max(0.0, w.end - 0.5)))
    return out
