import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# Классическое скачивание пишет файл модели постепенно — так видно прогресс (Xet пишет только в конце)
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
JOBS_DIR = DATA_DIR / "jobs"
WEB_DIR = ROOT / "web"
JOBS_DIR.mkdir(parents=True, exist_ok=True)

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3-turbo")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "auto")  # auto: видеокарта NVIDIA, если есть
LANGUAGE = os.getenv("LANGUAGE", "ru")

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5")
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "medium")

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8765"))


def _find_tool(name: str) -> str:
    env = os.getenv(f"{name.upper()}_PATH")
    if env:
        return env
    found = shutil.which(name)
    if found:
        return found
    # winget ставит ffmpeg сюда; PATH может обновиться только после перезапуска терминала
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for base in (local / "Microsoft" / "WinGet" / "Links", local / "Microsoft" / "WinGet" / "Packages"):
        if base.exists():
            for exe in base.rglob(f"{name}.exe"):
                return str(exe)
    raise RuntimeError(f"{name} не найден. Установите ffmpeg или укажите {name.upper()}_PATH в .env")


FFMPEG = _find_tool("ffmpeg")
FFPROBE = _find_tool("ffprobe")


def has_claude_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
