"""Поиск того, что нужно вырезать: мычание (правила), дубли/оговорки/паразиты (Claude или локальные правила)."""
import json
import re
from concurrent.futures import ThreadPoolExecutor

import anthropic

from . import local_rules
from .config import CLAUDE_EFFORT, CLAUDE_MODEL, has_claude_key

# Чем выше, тем важнее: если слово попало в несколько отметок, побеждает более «сильная»
PRIORITY = {"filler": 1, "parasite": 2, "stumble": 3, "retake": 4}

CHUNK_WORDS = 2500
CHUNK_OVERLAP = 300

SYSTEM_PROMPT = """Ты помогаешь монтажёру чистить черновую запись речи (видеоблог, урок, выступление). \
Тебе дают расшифровку, где у каждого слова есть номер: «12:слово». Паузы длиннее секунды помечены как (пауза 2.3с).

Найди куски, которые монтажёр вырезал бы, и верни их диапазонами номеров слов (from и to включительно):

- retake: человек сбился и начал фразу или абзац заново. Вырежи неудачную попытку целиком: от начала \
неудачного варианта до слова перед началом удачного. Реплики вроде «так, заново», «стоп», «ещё раз», «блин» \
между попытками тоже входят в вырез. Оставляется последний, полный вариант.
- stumble: оговорка в пределах фразы: оборванное слово, заикание, тут же исправленное слово \
(«на пятн… на шестой», «я я я думаю»). Вырезай только лишнее, чтобы оставшееся читалось гладко.
- parasite: слова-паразиты, которые не несут смысла в этом месте: «ну», «вот», «типа», «как бы», \
«короче», «это самое», «в общем», «значит», «так сказать». Будь осторожен: «вот этот файл», «как бы \
вы поступили», «значит, x равен двум» — это смысловые слова, их не трогай.
- filler: мычание и междометия колебания («э», «ээм», «мм»), если они ещё остались.

Правила:
- Не переписывай и не сокращай текст по смыслу: вырезается только брак, содержание остаётся.
- Если сомневаешься, лучше не вырезай.
- После вырезания оставшиеся слова должны складываться в грамотную связную речь.
- reason: коротко по-русски, что именно не так (например «повтор: удачный вариант с 245»).
- Если вырезать нечего, верни пустой список."""

SCHEMA = {
    "type": "object",
    "properties": {
        "cuts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "from": {"type": "integer"},
                    "to": {"type": "integer"},
                    "type": {"type": "string", "enum": ["retake", "stumble", "parasite", "filler"]},
                    "reason": {"type": "string"},
                },
                "required": ["from", "to", "type", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["cuts"],
    "additionalProperties": False,
}


def _format_chunk(words: list[dict]) -> str:
    parts = []
    prev_end = None
    for w in words:
        if prev_end is not None and w["s"] - prev_end >= 1.0:
            parts.append(f"(пауза {w['s'] - prev_end:.1f}с)")
        parts.append(f"{w['i']}:{w['w']}")
        prev_end = w["e"]
    return " ".join(parts)


def _ask_claude(client: anthropic.Anthropic, words: list[dict]) -> list[dict]:
    with client.beta.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        thinking={"type": "adaptive"},
        output_config={"effort": CLAUDE_EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _format_chunk(words)}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError("Claude отказался обрабатывать этот фрагмент")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Ответ Claude обрезан по лимиту токенов")
    text = next(b.text for b in msg.content if b.type == "text")
    lo, hi = words[0]["i"], words[-1]["i"]
    cuts = []
    for c in json.loads(text)["cuts"]:
        a, b = max(lo, min(c["from"], c["to"])), min(hi, max(c["from"], c["to"]))
        if a <= b:
            cuts.append({**c, "from": a, "to": b, "source": "claude"})
    return cuts


def claude_marks(words: list[dict], on_progress=lambda p: None) -> list[dict]:
    if not words:
        return []
    client = anthropic.Anthropic(max_retries=4)
    chunks, start = [], 0
    while True:  # куски с перекрытием, чтобы не потерять дубль на стыке
        chunks.append(words[start:start + CHUNK_WORDS])
        if start + CHUNK_WORDS >= len(words):
            break
        start += CHUNK_WORDS - CHUNK_OVERLAP
    done = 0
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for cuts in pool.map(lambda ch: _ask_claude(client, ch), chunks):
            results.extend(cuts)
            done += 1
            on_progress(done / len(chunks))
    return results


def analyze(words: list[dict], on_progress=lambda p: None) -> dict:
    marks = local_rules.analyze_local(words, with_speech_rules=False)
    claude_error = None
    if not has_claude_key():
        claude_error = ("Claude не подключён — паразиты, оговорки и дубли найдены упрощённым локальным поиском. "
                        "Проверьте разметку внимательнее")
    else:
        try:
            marks += claude_marks(words, on_progress)
        except anthropic.AuthenticationError:
            claude_error = "Неверный ANTHROPIC_API_KEY"
        except anthropic.RateLimitError:
            claude_error = "Превышен лимит запросов Claude, попробуйте повторить анализ позже"
        except anthropic.APIConnectionError:
            claude_error = "Нет связи с Claude API"
        except anthropic.APIStatusError as e:
            claude_error = f"Ошибка Claude API ({e.status_code}): {e.message}"
        except Exception as e:  # noqa: BLE001 — ошибка анализа не должна ронять весь процесс
            claude_error = f"Ошибка анализа Claude: {e}"
    if claude_error:  # Claude не сработал — подстраховываемся локальными правилами
        marks = local_rules.analyze_local(words)
        if has_claude_key():
            claude_error += ". Использован упрощённый локальный поиск"

    # Раскладываем отметки по словам: у каждого слова одна метка с наивысшим приоритетом
    tags: dict[int, int] = {}
    for mi, m in enumerate(marks):
        m["id"] = mi
        for i in range(m["from"], m["to"] + 1):
            cur = tags.get(i)
            if cur is None or PRIORITY[m["type"]] > PRIORITY[marks[cur]["type"]]:
                tags[i] = mi
    return {"marks": marks, "word_marks": {str(k): v for k, v in tags.items()}, "claude_error": claude_error}
