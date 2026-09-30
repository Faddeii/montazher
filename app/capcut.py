"""Экспорт монтажа прямо в CapCut (ПК): создаёт проект, где каждый оставленный кусок —
отдельный клип на таймлайне. Проект сразу появляется в списке проектов CapCut.

Формат черновика CapCut не документирован, поэтому за основу взят шаблон настоящего проекта
(app/capcut_template.json): подставляем в него свои куски, ничего не выдумывая.
"""
import copy
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

TEMPLATE = Path(__file__).with_name("capcut_template.json")
US = 1_000_000


def drafts_dir() -> Path | None:
    env = os.getenv("CAPCUT_DRAFTS")
    if env:
        return Path(env)
    d = Path(os.environ.get("LOCALAPPDATA", "")) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"
    return d if d.exists() else None


def _uid() -> str:
    return str(uuid.uuid4()).upper()


def _fwd(p: Path) -> str:
    return str(p).replace("\\", "/")


def _safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip()[:60] or "Монтаж"


def build_draft(src: Path, title: str, info: dict, segments: list[tuple[float, float]]) -> Path:
    root = drafts_dir()
    if root is None:
        raise RuntimeError("CapCut не найден на этом компьютере. Установите CapCut для ПК или укажите CAPCUT_DRAFTS в .env")
    tpl = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    src = src.resolve()

    # Папка проекта: «Монтажёр — имя», при совпадении добавляем номер
    base = _safe_name(f"Монтажёр — {Path(title).stem}")
    name, n = base, 2
    while (root / name).exists():
        name, n = f"{base} ({n})", n + 1
    folder = root / name
    folder.mkdir(parents=True)

    fps = info["fps"] if info["has_video"] else 30.0
    src_us = int(info["duration"] * US)
    local_id = str(uuid.uuid4())
    now_s, now_us = int(time.time()), int(time.time() * US)

    content = tpl["content"]
    content.update({"id": _uid(), "fps": float(round(fps, 3)), "name": "", "create_time": now_s, "update_time": now_s})
    content["canvas_config"].update({"width": info["width"] or 1920, "height": info["height"] or 1080, "ratio": "original"})
    mats = content["materials"]
    track = tpl["track"]
    track["id"] = _uid()

    pos = 0
    for s, e in segments:
        start, dur = int(round(s * US)), int(round((e - s) * US))
        if dur <= 0:
            continue
        video = copy.deepcopy(tpl["video"])
        video.update({"id": _uid(), "path": _fwd(src), "material_name": src.name, "duration": src_us,
                      "width": info["width"], "height": info["height"], "has_audio": True,
                      "local_material_id": local_id, "type": "video" if info["has_video"] else "audio"})
        video["video_algorithm"]["time_range"] = {"start": 0, "duration": src_us}
        mats["videos"].append(video)

        refs = []
        for ex in tpl["extras"]:
            m = copy.deepcopy(ex["material"])
            m["id"] = _uid()
            if ex["key"] == "loudnesses":
                m["time_range"] = {"start": start, "duration": dur}
            mats.setdefault(ex["key"], []).append(m)
            refs.append(m["id"])

        seg = copy.deepcopy(tpl["segment"])
        seg.update({"id": _uid(), "material_id": video["id"], "extra_material_refs": refs,
                    "source_timerange": {"start": start, "duration": dur},
                    "target_timerange": {"start": pos, "duration": dur}})
        track["segments"].append(seg)
        pos += dur

    content["tracks"] = [track]
    content["duration"] = pos
    (folder / "draft_content.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    meta = tpl["meta"]
    mm = tpl["meta_material"]
    mm.update({"create_time": now_s, "duration": src_us, "extra_info": src.name, "file_Path": _fwd(src),
               "height": info["height"], "width": info["width"], "id": local_id, "import_time": now_s,
               "import_time_ms": now_us, "roughcut_time_range": {"duration": src_us, "start": 0},
               "metetype": "video" if info["has_video"] else "music"})
    meta["draft_materials"][0]["value"] = [mm]
    meta.update({"draft_fold_path": _fwd(folder), "draft_id": _uid(), "draft_name": name,
                 "draft_root_path": str(root), "tm_draft_create": now_us, "tm_draft_modified": now_us,
                 "tm_duration": pos, "draft_cover": "", "draft_timeline_materials_size_": 0})
    for key in ("tm_draft_cloud_completed", "cloud_package_completed_time"):
        meta[key] = ""
    (folder / "draft_meta_info.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    vstore = tpl["virtual_store"]
    for group in vstore["draft_virtual_store"]:
        if group["type"] == 1:
            group["value"] = [{"child_id": local_id, "parent_id": ""}]
    (folder / "draft_virtual_store.json").write_text(json.dumps(vstore, ensure_ascii=False), encoding="utf-8")

    _register(root, folder, meta, pos)
    return folder


def _register(root: Path, folder: Path, meta: dict, duration_us: int) -> None:
    """Добавляет проект в общий список CapCut, чтобы он сразу был виден на главном экране."""
    path = root / "root_meta_info.json"
    if not path.exists():
        return
    backup = root / "root_meta_info.json.montazher.bak"
    if not backup.exists():
        shutil.copy2(path, backup)
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = copy.deepcopy(json.loads(TEMPLATE.read_text(encoding="utf-8"))["root_entry"])
    entry.update({"draft_cover": "", "draft_fold_path": _fwd(folder), "draft_id": meta["draft_id"],
                  "draft_json_file": _fwd(folder / "draft_content.json"), "draft_name": meta["draft_name"],
                  "draft_root_path": str(root), "draft_timeline_materials_size": 0,
                  "tm_draft_create": meta["tm_draft_create"], "tm_draft_modified": meta["tm_draft_modified"],
                  "tm_draft_removed": 0, "tm_duration": duration_us})
    data["all_draft_store"].insert(0, entry)
    data["draft_ids"] = data.get("draft_ids", 0) + 1
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
