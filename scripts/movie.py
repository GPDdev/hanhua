import os
import re

import numpy as np
import pysubs2
import soundfile as sf
from opencc import OpenCC

from core import atomic, chinese_track, ff, language, probe, put, read, unit
from sound import assign_speakers, hear, separate, voice_track
from vision import alike, screen_cues

TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt"}
BED = re.compile(r"music\s*(?:and|&|\+)\s*(?:effects|sfx)|\bm\s*&\s*e\b|音乐音效", re.I)
ASIDE = re.compile(r"^(?:[♪♫].*|[\[（(【].*[\]）)】])$")


def audio_streams(info):
    items = [s for s in info["streams"] if s["codec_type"] == "audio"]
    background = next((s for s in items if BED.search(s.get("tags", {}).get("title", ""))), None)
    candidates = [s for s in items if s is not background and not s.get("disposition", {}).get("comment")]
    zh = next((s for s in candidates if chinese_track(s.get("tags", {}), audio=True)), None)
    original = next(
        (s for s in candidates if s is not zh and s.get("disposition", {}).get("default")), None
    )
    original = original or next((s for s in candidates if s is not zh), None) or zh
    return original, zh, background


def sidecars(movie):
    return sorted(
        p
        for p in movie.parent.iterdir()
        if p.is_file()
        and p.suffix.lower() in SUB_EXT
        and (p.stem == movie.stem or p.stem.startswith(movie.stem + "."))
    )


def read_subs(p):
    entries = None
    for enc in ("utf-8-sig", "utf-16", "gb18030", "cp1252"):
        try:
            entries = pysubs2.load(str(p), encoding=enc)
            break
        except (UnicodeError, ValueError):
            continue
    if entries is None:
        raise ValueError("Unreadable subtitles: " + str(p))
    cues = []
    for line in entries:
        if line.is_comment or line.is_drawing or line.end <= line.start:
            continue
        text = line.plaintext.strip()
        if text:
            cues.append(
                dict(
                    start=line.start,
                    end=line.end,
                    text=text,
                    speaker=line.name or "",
                    kind="screen" if ASIDE.fullmatch(text) else "dialogue",
                )
            )
    return cues


def subtitle_inputs(movie, info, work, notes):
    opts = []

    def caption_tag(tags):
        tag = language(tags.get("language"))
        title = tags.get("title", "").lower()
        traditional = tag in ("zh-tw", "zh-hk", "zh-hant", "cht") or any(
            t in title for t in ("繁体", "繁體", "traditional", "zh-hant", "zh-tw")
        )
        return chinese_track(tags) or traditional, traditional

    for p in sidecars(movie):
        tag = p.stem[len(movie.stem) :].strip(".").lower()
        zh, traditional = caption_tag({"language": tag, "title": tag})
        opts.append((int(traditional), zh, p, None, not tag))
    for s in info["streams"]:
        if (
            s["codec_type"] != "subtitle"
            or s.get("disposition", {}).get("forced")
            or s.get("disposition", {}).get("comment")
        ):
            continue
        tags = s.get("tags", {})
        zh, traditional = caption_tag(tags)
        opts.append((int(traditional), zh, None, s, language(tags.get("language")) in ("", "und")))
    result = {}
    for _, is_zh, p, stream, untagged in sorted(opts, key=lambda x: x[0]):
        if is_zh in result and not untagged:
            continue
        try:
            if stream and stream["codec_name"] not in TEXT_SUBS:
                cues = screen_cues(
                    movie,
                    info,
                    stream["index"],
                    lang=stream.get("tags", {}).get("language", ""),
                    checkpoint=work / f"ocr-{stream['index']}.json",
                )
                for c in cues:
                    c["kind"] = "dialogue" if not ASIDE.fullmatch(c["text"]) else "screen"
            else:
                if stream:
                    p = work / f"source-{stream['index']}.ass"
                    ff("-i", movie, "-map", f"0:{stream['index']}", "-c:s", "ass", p)
                cues = read_subs(p)
            if cues:
                if untagged:
                    sample = " ".join(c["text"] for c in cues[:35])
                    if len(re.findall(r"[\u3400-\u9fff]", sample)) > len(
                        re.findall(r"[a-zA-Z\u3040-\u30ff\uac00-\ud7a3]", sample)
                    ):
                        is_zh = True
                if is_zh:
                    conv = OpenCC("t2s")
                    for c in cues:
                        c["text"] = conv.convert(c["text"])
                result.setdefault(is_zh, cues)
        except (ValueError, RuntimeError, OSError) as e:
            notes.append("字幕轨读取失败 " + str(e))
    return result


def mono_audio(movie, stream, work):
    path = work / "speech.wav"
    if not path.is_file():
        tmp = work / "speech.pending.wav"
        ff(
            "-i",
            movie,
            "-map",
            f"0:{stream['index']}",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            tmp,
        )
        os.replace(tmp, path)
    return path


def closest(cue, others):
    a, b = cue["start"], cue["end"]
    matches = [
        (min(b, c["end"]) - max(a, c["start"]), i, c)
        for i, c in enumerate(others)
        if a < c["end"] and c["start"] < b
    ]
    return max(
        matches, default=(0, -1, None), key=lambda x: x[0] / max(1, b - a, x[2]["end"] - x[2]["start"])
    )


def prepare(source, work):
    info = probe(source)
    duration = float(info["format"]["duration"])
    original, zh_audio, bed = audio_streams(info)
    notes = []
    subs = subtitle_inputs(source, info, work, notes)
    source_cues = subs.get(False, [])
    existing = subs.get(True, [])
    lang = language((original or {}).get("tags", {}).get("language"))
    transcript_mode = "原文字幕"
    if not source_cues and original and (not existing or original != zh_audio):
        wav = mono_audio(source, original, work)
        cached = work / "speech-cues.json"
        if cached.is_file():
            source_cues, lang = read(cached)
        else:
            source_cues, lang = hear(wav, language=lang)
            put(cached, [source_cues, lang])
        transcript_mode = "语音听译"
    if not source_cues and existing:
        source_cues = [dict(c) for c in existing]
        transcript_mode = "已有中文字幕"
    if original and any(c["kind"] == "dialogue" and not c["speaker"] for c in source_cues):
        try:
            assign_speakers(
                mono_audio(source, original, work), [c for c in source_cues if c["kind"] == "dialogue"]
            )
        except (ValueError, RuntimeError, OSError) as e:
            notes.append("未分辨的角色采用旁白音色 " + str(e))
    visible = screen_cues(source, info, lang=lang, checkpoint=work / "ocr-picture.json")
    if not any(c["kind"] == "dialogue" for c in source_cues):
        lower = [c for c in visible if c.get("caption_band") and 650 <= c["end"] - c["start"] <= 12500]
        if len({c["start"] for c in lower}) >= 2:
            for c in lower:
                c["kind"] = "dialogue"
            source_cues.extend(lower)
            visible = [c for c in visible if c["kind"] != "dialogue"]
            transcript_mode = "画面硬字幕"
    for s in info["streams"]:
        if s["codec_type"] != "subtitle" or not s.get("disposition", {}).get("forced"):
            continue
        try:
            if s["codec_name"] in TEXT_SUBS:
                path = work / f"forced-{s['index']}.ass"
                ff("-i", source, "-map", f"0:{s['index']}", "-c:s", "ass", path)
                extra = read_subs(path)
            else:
                extra = screen_cues(source, info, s["index"], lang=s.get("tags", {}).get("language", lang))
            for c in extra:
                c["kind"] = "screen"
            visible.extend(extra)
        except (ValueError, OSError, RuntimeError) as e:
            notes.append("强制字幕轨读取失败 " + str(e))
    timeline = source_cues.copy()
    for c in visible:
        if any(
            alike(c["text"], s["text"]) and c["start"] < s["end"] + 500 and s["start"] < c["end"] + 500
            for s in timeline + existing
        ):
            continue
        timeline.append(c)
    units = []
    cues = []
    used = set()
    for c in sorted(timeline, key=lambda x: (x["start"], x["kind"])):
        c = dict(c)
        c["start"] = max(0, c["start"])
        c["end"] = min(round(duration * 1000), c["end"])
        if c["end"] <= c["start"]:
            continue
        known = None
        overlap, i, target = closest(c, existing)
        if (
            target
            and i not in used
            and overlap >= 0.65 * max(c["end"] - c["start"], target["end"] - target["start"])
        ):
            known = target["text"]
            used.add(i)
        if c["kind"] == "dialogue":
            if transcript_mode == "已有中文字幕" or (
                transcript_mode == "语音听译" and lang in ("zh", "cmn", "zho", "chi", "zh-cn")
            ):
                known = c["text"]
        u = unit(
            c["text"], "影视对白" if c["kind"] == "dialogue" else "画面文字", c["speaker"], c["kind"], known
        )
        units.append(u)
        c["id"] = u["id"]
        cues.append(c)
    return dict(
        kind="movie",
        duration=duration,
        units=units,
        cues=cues,
        original=original["index"] if original else None,
        zh_audio=zh_audio["index"] if zh_audio else None,
        bed=bed["index"] if bed else None,
        transcript=transcript_mode,
        notes=notes,
    )


def subfile(cues, trans, bilingual, file):
    subs = pysubs2.SSAFile()
    subs.info.update({"PlayResX": "1280", "PlayResY": "720", "WrapStyle": "0"})
    subs.styles["Default"] = pysubs2.SSAStyle(
        fontname="Microsoft YaHei", fontsize=36, outline=2, marginv=35
    )
    subs.styles["Screen"] = pysubs2.SSAStyle(
        fontname="Microsoft YaHei", fontsize=29, alignment=8, outline=2, marginv=22
    )
    for c in cues:
        zh = trans[c["id"]]
        text = zh + "\n" + c["text"] if bilingual and zh.strip() != c["text"].strip() else zh
        e = pysubs2.SSAEvent(
            start=c["start"], end=c["end"], style="Screen" if c["kind"] == "screen" else "Default"
        )
        e.plaintext = text
        subs.append(e)
    atomic(file, subs.to_string("ass" if bilingual else "srt").encode("utf-8-sig"))


def build(source, plan, trans, out, work):
    notes = list(plan["notes"])
    cues = plan["cues"]
    prefix = source.stem
    zh = out / (prefix + ".zh-Hans.srt")
    dual = out / (prefix + ".bilingual.ass")
    subfile(cues, trans, False, zh)
    subfile(cues, trans, True, dual)
    roles = {}
    mixed = None
    mode = "已有普通话轨"
    dialogue = any(c["kind"] == "dialogue" for c in cues)
    if plan["zh_audio"] is None and dialogue:
        rendered = work / "voice.wav"
        roles = voice_track(cues, trans, work / "voices", plan["duration"], rendered)
        mixed = work / "mandarin.m4a"
        bg = None
        gain = 1
        if plan["bed"] is not None:
            bg = work / "bed.wav"
            ff("-i", source, "-map", f"0:{plan['bed']}", "-ac", "2", "-ar", "44100", bg)
            mode = "独立音乐音效轨配音"
        elif plan["original"] is not None:
            original = work / "original.wav"
            ff("-i", source, "-map", f"0:{plan['original']}", "-ar", "44100", "-ac", "2", original)
            try:
                bg = work / "separated.wav"
                separate(original, bg)
                # Reject a stem that simply copied the mix during spoken portions.
                readings = []
                with sf.SoundFile(str(original)) as a, sf.SoundFile(str(bg)) as b:
                    for c in [v for v in cues if v["kind"] == "dialogue"][:20]:
                        pos = min(len(a), int(c["start"] * 44.1))
                        a.seek(pos)
                        b.seek(min(pos, len(b)))
                        x = a.read(22050, dtype="float32")
                        y = b.read(len(x), dtype="float32")
                        if x.size and len(y) == len(x) and np.mean(x * x) > 1e-6:
                            readings.append(float(np.sum(x * y) / np.sum(x * x)))
                if readings and np.median(readings) > 0.83:
                    raise ValueError("对白残留较强")
                mode = "分离背景音后的普通话配音"
            except (RuntimeError, ValueError, OSError) as e:
                bg = original
                gain = 0.19
                mode = "原声压低的中文旁白"
                notes.append("背景分离回退 " + str(e))
        else:
            mode = "普通话配音"
        if bg:
            ff(
                "-i",
                bg,
                "-i",
                rendered,
                "-filter_complex",
                f"[0:a]volume={gain}[b];[b][1:a]amix=inputs=2:normalize=0:duration=longest,alimiter=limit=0.96:latency=1[a]",
                "-map",
                "[a]",
                "-t",
                plan["duration"],
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                mixed,
            )
        else:
            ff("-i", rendered, "-c:a", "aac", "-b:a", "160k", mixed)
    elif plan["zh_audio"] is None:
        mode = "仅画面文字或未识别到对白"
    info = probe(source)
    aud = [s for s in info["streams"] if s["codec_type"] == "audio"]
    subs = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    args = ["-i", source]
    if mixed:
        args += ["-i", mixed]
    # An unrecognizable/silent source may have no cues; empty external subtitles stay readable.
    if cues:
        args += ["-i", zh, "-i", dual]
    args += ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?"]
    if mixed:
        args += ["-map", "1:a:0"]
    if cues:
        start = 2 if mixed else 1
        args += ["-map", f"{start}:s:0", "-map", f"{start + 1}:s:0"]
    args += ["-map_metadata", "0", "-map_chapters", "0", "-c", "copy"]
    for i, s in enumerate(subs):
        if s["codec_name"] in {"mov_text", "webvtt", "text"}:
            args += [f"-c:s:{i}", "srt"]
    if mixed:
        args += [
            f"-metadata:s:a:{len(aud)}",
            "language=chi",
            f"-metadata:s:a:{len(aud)}",
            "title=普通话 | " + mode,
        ]
    if mixed or plan["zh_audio"] is not None:
        for i, s in enumerate(aud):
            args += [f"-disposition:a:{i}", "+default" if s["index"] == plan["zh_audio"] else "-default"]
        if mixed:
            args += [f"-disposition:a:{len(aud)}", "+default"]
    if cues:
        for i in range(len(subs)):
            args += [f"-disposition:s:{i}", "-default"]
        for i, title in enumerate(("简体中文", "中外双语"), len(subs)):
            args += [f"-metadata:s:s:{i}", "language=chi", f"-metadata:s:s:{i}", "title=" + title]
        args += [f"-disposition:s:{len(subs)}", "0", f"-disposition:s:{len(subs) + 1}", "default"]
    target = out / (prefix + ".zh-Hans.mkv")
    tmp = work / "mux.pending.mkv"
    ff(*args, tmp)
    got = probe(tmp)
    if len([s for s in got["streams"] if s["codec_type"] == "audio"]) != len(aud) + bool(mixed):
        raise ValueError("Mux verification failed")
    os.replace(tmp, target)
    return dict(
        result=str(target),
        subtitle=str(zh),
        bilingual=str(dual),
        dialogue_source=plan["transcript"],
        audio_mode=mode,
        voices=roles,
        dialogue=sum(c["kind"] == "dialogue" for c in cues),
        screen=sum(c["kind"] == "screen" for c in cues),
        notes=notes,
    )
