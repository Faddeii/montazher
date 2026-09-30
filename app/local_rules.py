"""Бесплатный локальный поиск паразитов, оговорок и дублей — без Claude, на правилах.

Работает по расшифровке Whisper: он ставит запятые вокруг вводных слов, помечает оборванные
слова многоточием или дефисом — на эти признаки и опираемся. Правила осторожные:
лучше пропустить паразита, чем вырезать смысловое слово.
"""
import re


def norm(word: str) -> str:
    return re.sub(r"[^\w-]", "", word.lower()).replace("ё", "е").strip("-")


HESITATION = re.compile(r"^(э+|м+|э+м+|а{2,}м*|м{2,}|х+м+|у{2,}|ы+|э-э+|а-а+|м-м+|э+-м+)$")

# Паразиты режем, только если они обособлены: стоят в начале фразы или после запятой
# и сами заканчиваются знаком препинания/паузой. «Вот этот файл» и «как бы вы поступили» не трогаем.
PARASITE_PHRASES = [
    "ну", "вот", "типа", "короче", "собственно", "вообще-то", "слушай", "понимаешь",
    "как бы", "это самое", "в общем", "в общем-то", "так сказать", "в принципе", "скажем так",
    "как говорится", "короче говоря", "что называется", "грубо говоря", "так вот", "ну вот",
    "как его", "как её", "как ее", "это", "вот это",
]
PARASITES = sorted((tuple(p.split()) for p in PARASITE_PHRASES), key=len, reverse=True)

# Слова, которые нормально повторять подряд («да да», «очень очень»)
OK_DOUBLES = {"да", "нет", "ну", "так", "очень", "давай", "еще", "ха", "ой", "ага", "тук", "вот", "по", "чуть"}

# Реплики, которыми человек сам себя останавливает перед новой попыткой
RESTART_CUES = [("заново",), ("еще", "раз"), ("стоп",), ("сначала",), ("дубль",), ("по-новой",), ("по", "новой")]

RETAKE_WINDOW = 40     # как далеко (в словах) ищем повтор фразы
RETAKE_MIN_WORDS = 3   # сколько слов подряд должно совпасть; с репликой «заново» хватает двух


def _clause_start(words, i) -> bool:
    return i == 0 or re.search(r"[,.!?;:…—-]$", words[i - 1]["w"]) is not None or words[i]["s"] - words[i - 1]["e"] > 0.4


def _clause_end(words, i) -> bool:
    return (re.search(r"[,.!?;:…—]$", words[i]["w"]) is not None or i == len(words) - 1
            or words[i + 1]["s"] - words[i]["e"] > 0.3)


def fillers(words) -> list[dict]:
    return [{"from": w["i"], "to": w["i"], "type": "filler",
             "reason": "мычание (найдено по звуку)" if w.get("synthetic") else "мычание", "source": "rules"}
            for w in words if w.get("synthetic") or HESITATION.match(norm(w["w"]))]


def parasites(words, taken: set[int]) -> list[dict]:
    marks = []
    n = [norm(w["w"]) for w in words]
    i = 0
    while i < len(words):
        for p in PARASITES:
            j = i + len(p) - 1
            if j >= len(words) or tuple(n[i:j + 1]) != p or taken & set(range(i, j + 1)):
                continue
            # одиночное «это» — только если оно явно вставлено между запятыми
            eto_ok = p != ("это",) or (i > 0 and words[i - 1]["w"].endswith(",") and words[j]["w"].endswith(","))
            if eto_ok and _clause_start(words, i) and _clause_end(words, j):
                marks.append({"from": i, "to": j, "type": "parasite",
                              "reason": f"«{' '.join(p)}» как вводное слово", "source": "rules"})
                i = j
                break
        i += 1
    return marks


def stumbles(words, taken: set[int]) -> list[dict]:
    marks = []
    n = [norm(w["w"]) for w in words]
    for i in range(len(words) - 1):
        if i in taken or not n[i]:
            continue
        # «я я думаю» — повтор слова подряд: оставляем последнее
        if n[i] == n[i + 1] and n[i] not in OK_DOUBLES:
            marks.append({"from": i, "to": i, "type": "stumble", "reason": f"повтор «{words[i]['w']}»", "source": "rules"})
            continue
        # «фай... файл», «пере- переделали» — оборванное слово, следом полное
        cut_off = re.search(r"(\.\.\.|…|-)$", words[i]["w"]) or (words[i].get("p", 1) < 0.5 and len(n[i]) >= 3)
        if cut_off and len(n[i]) >= 2:
            for j in range(i + 1, min(i + 4, len(words))):
                if n[j] != n[i] and n[j].startswith(n[i]):
                    marks.append({"from": i, "to": j - 1, "type": "stumble",
                                  "reason": f"оборванное слово, дальше «{words[j]['w']}»", "source": "rules"})
                    break
    return marks


def retakes(words, taken: set[int]) -> list[dict]:
    """Человек сбился и начал фразу заново: ищем одинаковую последовательность слов,
    повторённую неподалёку, и вырезаем всё от первой попытки до начала второй."""
    # сравниваем без мычания и паразитов: «потом мы, ну, выбираем» == «потом мы выбираем»
    seq = [(i, norm(w["w"])) for i, w in enumerate(words) if i not in taken and norm(w["w"])]
    toks = [t for _, t in seq]
    marks = []
    a = 0
    while a < len(seq):
        found = None
        for b in range(a + 1, min(a + RETAKE_WINDOW, len(seq))):
            k = 0
            while b + k < len(seq) and a + k < b and toks[a + k] == toks[b + k]:
                k += 1
            if k == 0:
                continue
            between = toks[a:b]
            cue = any(tuple(between[x:x + len(c)]) == c for c in RESTART_CUES for x in range(len(between)))
            # без «стоп/заново» первая попытка должна быть оборвана, иначе это обычный
            # ораторский повтор: «Мы говорим о монтаже. Мы говорим о звуке.»
            last = words[seq[b - 1][0]]["w"]
            unfinished = not re.search(r"[.!?]$", last) or re.search(r"(\.\.\.|…)$", last)
            if (k >= RETAKE_MIN_WORDS and unfinished) or (k >= 2 and cue):
                found = (b, k)
                break
        if found:
            b, k = found
            start, end = seq[a][0], seq[b][0] - 1
            marks.append({"from": start, "to": end, "type": "retake",
                          "reason": f"повтор фразы «{' '.join(toks[b:b + k])}» — оставлен второй вариант",
                          "source": "rules"})
            a = b
        else:
            a += 1
    return marks


def analyze_local(words, with_speech_rules: bool = True) -> list[dict]:
    marks = fillers(words)
    if not with_speech_rules:
        return marks
    taken = {i for m in marks for i in range(m["from"], m["to"] + 1)}
    marks += parasites(words, taken)
    taken = {i for m in marks for i in range(m["from"], m["to"] + 1)}
    marks += stumbles(words, taken)
    taken = {i for m in marks for i in range(m["from"], m["to"] + 1)}
    marks += retakes(words, taken)
    return marks
