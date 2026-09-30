"""Экспорт монтажа для Premiere Pro / DaVinci Resolve: FCP7 XML и EDL (CMX3600)."""
from pathlib import Path
from urllib.parse import quote
from xml.sax.saxutils import escape


def _timebase(info: dict) -> tuple[int, bool, float]:
    """Возвращает (timebase, ntsc, реальная частота кадров) в терминах FCP XML."""
    fps = info["fps"] if info["has_video"] else 30.0
    base = max(1, round(fps))
    ntsc = abs(fps - base * 1000 / 1001) < 0.01
    return base, ntsc, (base * 1000 / 1001 if ntsc else float(base))


def _frames(segments, rate: float):
    """Границы кусков в кадрах исходника + их положение на таймлайне без накопления ошибки."""
    out, pos = [], 0
    for s, e in segments:
        a, b = round(s * rate), round(e * rate)
        if b > a:
            out.append((a, b, pos, pos + (b - a)))
            pos += b - a
    return out, pos


def _pathurl(path: Path) -> str:
    return "file://localhost/" + quote(str(path.resolve()).replace("\\", "/"), safe="/:")


def fcp_xml(src: Path, name: str, info: dict, segments) -> str:
    base, ntsc, rate = _timebase(info)
    clips, total = _frames(segments, rate)
    src_frames = round(info["duration"] * rate)
    rate_xml = f"<rate><timebase>{base}</timebase><ntsc>{'TRUE' if ntsc else 'FALSE'}</ntsc></rate>"
    channels = min(2, info["channels"])
    title = escape(Path(name).stem)

    file_media = ""
    if info["has_video"]:
        file_media += (f"<video><samplecharacteristics>{rate_xml}<width>{info['width']}</width>"
                       f"<height>{info['height']}</height></samplecharacteristics></video>")
    file_media += (f"<audio><samplecharacteristics><depth>16</depth><samplerate>{info['sample_rate']}</samplerate>"
                   f"</samplecharacteristics><channelcount>{channels}</channelcount></audio>")
    file_full = (f'<file id="file-1"><name>{escape(name)}</name><pathurl>{_pathurl(src)}</pathurl>{rate_xml}'
                 f"<duration>{src_frames}</duration><media>{file_media}</media></file>")
    file_ref = '<file id="file-1"/>'

    def clipitem(cid: str, idx: int, a, b, t0, t1, audio_track: int | None, first: bool) -> str:
        links = []
        if info["has_video"]:
            links.append(f"<link><linkclipref>v-{idx}</linkclipref><mediatype>video</mediatype>"
                         f"<trackindex>1</trackindex><clipindex>{idx + 1}</clipindex></link>")
        for ch in range(1, channels + 1):
            links.append(f"<link><linkclipref>a{ch}-{idx}</linkclipref><mediatype>audio</mediatype>"
                         f"<trackindex>{ch}</trackindex><clipindex>{idx + 1}</clipindex></link>")
        src_track = (f"<sourcetrack><mediatype>audio</mediatype><trackindex>{audio_track}</trackindex></sourcetrack>"
                     if audio_track else "")
        return (f'<clipitem id="{cid}"><name>{title}</name><enabled>TRUE</enabled>'
                f"<duration>{src_frames}</duration>{rate_xml}<start>{t0}</start><end>{t1}</end>"
                f"<in>{a}</in><out>{b}</out>{file_full if first else file_ref}"
                f"{src_track}{''.join(links)}</clipitem>")

    first = True
    video_xml = ""
    if info["has_video"]:
        items = []
        for idx, (a, b, t0, t1) in enumerate(clips):
            items.append(clipitem(f"v-{idx}", idx, a, b, t0, t1, None, first))
            first = False
        video_xml = (f"<video><format><samplecharacteristics>{rate_xml}<width>{info['width']}</width>"
                     f"<height>{info['height']}</height><pixelaspectratio>square</pixelaspectratio>"
                     f"</samplecharacteristics></format><track>{''.join(items)}</track></video>")
    audio_tracks = []
    for ch in range(1, channels + 1):
        items = []
        for idx, (a, b, t0, t1) in enumerate(clips):
            items.append(clipitem(f"a{ch}-{idx}", idx, a, b, t0, t1, ch, first))
            first = False
        audio_tracks.append(f"<track>{''.join(items)}</track>")
    audio_xml = (f"<audio><numOutputChannels>{channels}</numOutputChannels><format><samplecharacteristics>"
                 f"<depth>16</depth><samplerate>{info['sample_rate']}</samplerate></samplecharacteristics></format>"
                 f"{''.join(audio_tracks)}</audio>")

    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n<xmeml version="4">'
            f'<sequence id="sequence-1"><name>{title} (монтаж)</name><duration>{total}</duration>{rate_xml}'
            f"<media>{video_xml}{audio_xml}</media></sequence></xmeml>\n")


def _tc(frames: int, base: int) -> str:
    f = frames % base
    s = frames // base
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def edl(name: str, info: dict, segments) -> str:
    base, _, rate = _timebase(info)
    clips, _ = _frames(segments, rate)
    start = 3600 * base  # таймлайн с 01:00:00:00, как принято в монтажках
    channel = "B" if info["has_video"] else "AA"
    lines = [f"TITLE: {Path(name).stem}", "FCM: NON-DROP FRAME", ""]
    for n, (a, b, t0, t1) in enumerate(clips, 1):
        lines.append(f"{n:03d}  AX       {channel:<6}C        {_tc(a, base)} {_tc(b, base)} "
                     f"{_tc(start + t0, base)} {_tc(start + t1, base)}")
        lines.append(f"* FROM CLIP NAME: {name}")
        lines.append("")
    return "\n".join(lines)
