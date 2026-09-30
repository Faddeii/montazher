"""Скачивание модели распознавания речи с видимым прогрессом.

Модель весит ≈1,6 ГБ и скачивается один раз. Без прогресса это выглядело как зависание,
поэтому качаем явно и считаем, сколько уже лежит на диске.

Запуск из консоли (так делает start.bat при установке): python -m app.model_store
"""
import threading
import time
from pathlib import Path
from typing import Callable

from .config import WHISPER_MODEL  # первым: загружает .env (HF_ENDPOINT) до импорта huggingface_hub

import huggingface_hub  # noqa: E402
from faster_whisper.utils import _MODELS, download_model  # noqa: E402

PATTERNS = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]


def _repo() -> str:
    return WHISPER_MODEL if "/" in WHISPER_MODEL else _MODELS[WHISPER_MODEL]


def model_ready() -> bool:
    try:
        download_model(WHISPER_MODEL, local_files_only=True)
        return True
    except Exception:  # noqa: BLE001 — нет в кэше или кэш битый
        return False


def _friendly(e: Exception) -> RuntimeError:
    return RuntimeError(
        "Не удалось скачать модель распознавания речи (≈1,6 ГБ) с huggingface.co. "
        "Проверьте интернет и попробуйте ещё раз. Если сайт у вас не открывается, включите VPN "
        f"или укажите зеркало в файле .env строкой HF_ENDPOINT=... (подробности в README). Ошибка: {e}")


def ensure_model(on_progress: Callable[[float], None] = lambda p: None) -> None:
    if model_ready():
        return
    repo = _repo()
    try:
        info = huggingface_hub.HfApi().model_info(repo, files_metadata=True)
        total = sum(s.size or 0 for s in info.siblings
                    if any(Path(s.rfilename).match(p) for p in PATTERNS)) or 1
    except Exception as e:  # noqa: BLE001
        raise _friendly(e) from e

    folder = Path(huggingface_hub.constants.HF_HUB_CACHE) / f"models--{repo.replace('/', '--')}"
    error: list[Exception] = []

    def worker():
        try:
            huggingface_hub.snapshot_download(repo, allow_patterns=PATTERNS)
        except Exception as e:  # noqa: BLE001
            error.append(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    while t.is_alive():
        done = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) if folder.exists() else 0
        on_progress(min(0.99, done / total))
        t.join(1.0)
    if error:
        raise _friendly(error[0]) from error[0]
    on_progress(1.0)


if __name__ == "__main__":
    if model_ready():
        print("  Модель распознавания уже скачана.")
    else:
        print("  Скачиваю модель распознавания речи (≈1,6 ГБ, один раз)...")
        started = time.monotonic()

        def show(p: float):
            print(f"\r  {p * 100:5.1f}%  ({time.monotonic() - started:.0f} с)", end="", flush=True)
        try:
            ensure_model(show)
            print("\n  Готово.")
        except RuntimeError as e:
            print(f"\n  {e}\n  Программа попробует скачать модель ещё раз при первой обработке видео.")
