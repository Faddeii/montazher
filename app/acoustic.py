"""Поиск мычания по звуку.

Whisper часто не пишет «эээ»/«ммм» в текст, а приклеивает их к соседнему слову: слово «у»
вдруг длится две секунды. Поэтому смотрим на сам звук: делим запись на звучащие «островки»,
каждому слову оставляем островок, который на него больше всего похож, а лишние голосовые
островки (тянущийся звук с высотой тона — это и есть мычание) превращаем в отдельные
«слова» «(э-э)», которые дальше вырезаются как мычание. Дыхание и щелчки — не голос, их не трогаем.

Каждый такой кусок дополнительно прослушивает Whisper: если он слышит «эээ»/«ммм» — это мычание,
если настоящие слова — это пропущенная речь, её возвращаем в текст, если шум — не трогаем.
"""
import re
from typing import Callable

import numpy as np

from .local_rules import norm
from .transcribe import HALLUCINATIONS

DRAWL = re.compile(r"([аэемуыи])\1\1")  # «эээ», «ааа», «ммм», «блэээ» — протяжный звук
CONFIDENT_SPEECH = -0.7  # порог уверенности Whisper, чтобы вернуть пропущенную речь в текст

HOP = 160           # 10 мс при 16 кГц
MERGE_GAP = 12      # островки с разрывом короче 120 мс — это один звук (смычка согласной)
MIN_FILLER = 20     # мычание короче 200 мс не ищем
VOICED_SHARE = 0.6  # доля кадров с высотой тона, чтобы считать островок голосом


def _frame_features(audio: np.ndarray):
    n = len(audio) // HOP
    frames = audio[: n * HOP].reshape(n, HOP)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    # Голос: сильный пик автокорреляции в диапазоне тона 80–400 Гц (окно 30 мс)
    win = 480
    padded = np.pad(audio, (0, win))
    idx = np.arange(n)[:, None] * HOP + np.arange(win)[None, :]
    voiced = np.zeros(n, dtype=bool)
    loud = db > np.percentile(db, 10) + 10
    for start in range(0, n, 4096):  # пачками, чтобы не съесть память на длинных записях
        sel = np.arange(start, min(n, start + 4096))
        sel = sel[loud[sel]]
        if not len(sel):
            continue
        x = padded[idx[sel]]
        x = x - x.mean(axis=1, keepdims=True)
        spec = np.fft.rfft(x, 2 * win, axis=1)
        ac = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, :win]
        ac = ac / (ac[:, :1] + 1e-9)
        voiced[sel] = ac[:, 40:200].max(axis=1) > 0.5
    return db, voiced


def _islands(active: np.ndarray) -> list[list[int]]:
    runs, start = [], None
    for i, a in enumerate(active):
        if a and start is None:
            start = i
        elif not a and start is not None:
            runs.append([start, i])
            start = None
    if start is not None:
        runs.append([start, len(active)])
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < MERGE_GAP:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [r for r in merged if r[1] - r[0] >= 5]


def _expected(word: str) -> float:
    """Примерная длительность слова в кадрах: ~70 мс на букву."""
    letters = sum(ch.isalpha() or ch.isdigit() for ch in word)
    return max(8, 7 * letters + 6)


def refine(words: list[dict], audio: np.ndarray, classify: Callable[[np.ndarray], tuple[str, float]],
           on_progress: Callable[[float], None] = lambda p: None) -> list[dict]:
    if not words or len(audio) < HOP * 10:
        return words
    db, voiced = _frame_features(audio)
    floor, speech = np.percentile(db, 10), np.percentile(db, 95)
    islands = _islands(db > floor + max(8.0, 0.3 * (speech - floor)))
    n = len(db)
    spans = [(w["s"], w["e"]) for w in words]  # исходные границы до уточнения

    owner = [None] * len(islands)  # кому достался островок: индекс слова
    for wi, w in enumerate(words):
        s, e = int(w["s"] * 100) - 5, int(np.ceil(w["e"] * 100)) + 5
        inside = [k for k, (a, b) in enumerate(islands) if a < e and b > s and owner[k] is None]
        if not inside:
            continue
        exp = _expected(w["w"])
        prev_end = words[wi - 1]["e"] * 100 if wi else -1e9
        next_start = words[wi + 1]["s"] * 100 if wi + 1 < len(words) else 1e9

        def score(k):
            a, b = islands[k]
            a, b = max(a, s), min(b, e)
            near = min(abs(a - prev_end), abs(next_start - b))  # слово обычно вплотную к соседям
            return abs((b - a) - exp) + 0.5 * min(near, 60)
        main = min(inside, key=score)
        keep = {main}
        # соседние островки ближе 250 мс, которые не похожи на тянущийся голос, — часть того же слова
        for k in sorted(inside, key=lambda k: abs(islands[k][0] - islands[main][0])):
            a, b = islands[k]
            if k in keep:
                continue
            close = min(abs(a - islands[main][1]), abs(islands[main][0] - b)) < 25
            filler_like = b - a >= MIN_FILLER and voiced[a:b].mean() >= VOICED_SHARE
            if close and not filler_like:
                keep.add(k)
        for k in keep:
            owner[k] = wi
        a = max(s + 5, min(islands[k][0] for k in keep))
        b = min(e - 5, max(islands[k][1] for k in keep))
        if b - a >= 5:
            w["s"], w["e"] = round(a / 100, 2), round(b / 100, 2)

    candidates = [(a, min(b, n)) for k, (a, b) in enumerate(islands)
                  if owner[k] is None and b - a >= MIN_FILLER and voiced[a:min(b, n)].mean() >= VOICED_SHARE]
    out = list(words)
    for ci, (a, b) in enumerate(candidates):
        on_progress((ci + 1) / len(candidates))
        t0, t1 = a / 100, b / 100
        text, confidence = classify(audio[max(0, a * HOP - 800): b * HOP + 800])
        tokens = [norm(x) for x in text.split() if norm(x)]
        if not tokens or HALLUCINATIONS.search(text):
            continue  # шум, музыка, кашель — не речь
        if any(DRAWL.search(t.replace("-", "")) for t in tokens):
            out.append({"w": "(э-э)", "s": round(t0, 2), "e": round(t1, 2), "p": 1.0, "synthetic": True})
            continue
        # Настоящая речь, которую Whisper пропустил в общем проходе, — возвращаем её в текст,
        # если он в ней уверен и она не пересекается с уже распознанными словами
        if confidence < CONFIDENT_SPEECH or any(s < t1 and e > t0 for s, e in spans):
            continue
        raw = text.split()
        total = sum(len(x) for x in raw) or 1
        t = t0
        for x in raw:
            d = (t1 - t0) * len(x) / total
            out.append({"w": x, "s": round(t, 2), "e": round(t + d, 2), "p": 0.5, "recovered": True})
            t += d
    out.sort(key=lambda w: w["s"])
    for i, w in enumerate(out):
        w["i"] = i
    return out


HUM = re.compile(r"^[аэмуыи]+$")  # токен целиком из тянущегося звука: «ээээ», «ммм»
MAX_DRAWN_LETTERS = 3             # хвост протяжки режем только у коротких слов: «и-и-и», «на-а-а»


def split_long_words(words: list[dict], audio: np.ndarray,
                     clip_words: Callable[[np.ndarray], list[tuple[str, float, float]]],
                     on_progress: Callable[[float], None] = lambda p: None) -> list[dict]:
    """Мычание вплотную к слову («на-эээ-ммм») и протянутые короткие слова («и-и-и»):
    слушаем каждое аномально длинное слово отдельно и выделяем из него лишнее."""
    long_words = [w for w in words if not w.get("synthetic")
                  and w["e"] - w["s"] > max(0.45, 2 * _expected(w["w"]) / 100)]
    out = list(words)
    pos = {id(w): n for n, w in enumerate(words)}
    for k, w in enumerate(long_words):
        on_progress((k + 1) / len(long_words))
        s, e = w["s"], w["e"]
        exp = min(_expected(w["w"]) / 100, (e - s) * 0.6)
        heard = clip_words(audio[int(s * 16000): int(e * 16000)])
        is_hum = [bool(HUM.match(norm(t).replace("-", "")) and DRAWL.search(norm(t))) for t, _, _ in heard]
        clamp = lambda t: min(max(t, s + exp), e - 0.15)  # таймкоды Whisper в коротком куске неточные
        if heard and all(is_hum):
            # слышно только мычание: само слово — короткий кусок со стороны соседнего слова
            n = pos[id(w)]
            gap_prev = s - words[n - 1]["e"] if n else 1e9
            gap_next = words[n + 1]["s"] - e if n + 1 < len(words) else 1e9
            if gap_next < gap_prev:
                w["s"], filler = round(e - exp, 2), (s, round(e - exp, 2))
            else:
                w["e"], filler = round(s + exp, 2), (round(s + exp, 2), e)
        elif any(is_hum):
            if is_hum[0]:   # мычание перед словом
                first_word = next(a for (t, a, b), h in zip(heard, is_hum) if not h)
                w["s"] = round(min(max(s + first_word, s + 0.15), e - exp), 2)
                filler = (s, w["s"])
            else:           # мычание после слова
                first_hum = next(a for (t, a, b), h in zip(heard, is_hum) if h)
                w["e"] = round(clamp(s + first_hum), 2)
                filler = (w["e"], e)
        elif (len(heard) == 1 and norm(heard[0][0]) == norm(w["w"]) and len(norm(w["w"])) <= MAX_DRAWN_LETTERS
              and heard[0][0].rstrip().endswith(("...", "…"))):
            w["e"] = round(s + max(0.2, 1.5 * _expected(w["w"]) / 100), 2)  # протянутое «и…»: оставляем начало
            filler = (w["e"], e)
        else:
            continue
        if filler[1] - filler[0] >= 0.15:
            out.append({"w": "(э-э)", "s": filler[0], "e": filler[1], "p": 1.0, "synthetic": True})
    out.sort(key=lambda x: x["s"])
    for i, x in enumerate(out):
        x["i"] = i
    return out
