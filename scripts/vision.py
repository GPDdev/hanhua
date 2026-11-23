import os
import re
import subprocess
from difflib import SequenceMatcher
from functools import lru_cache

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from core import MODELS, download, put, read, textlike

RECOGNIZERS = {
    "ja": ("japan", 4, "e1075a67dba758ecfc7ebc78a10ae61c95ac8fb66a9c86fab5541e33f085cb7a"),
    "ko": ("korean", 5, "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b"),
    "en": ("en", 5, "c3461add59bb4323ecba96a492ab75e06dda42467c9e3d0c18db5d1d21924be8"),
    "latin": ("latin", 5, "b20bd37c168a570f583afbc8cd7925603890efbcdc000a59e22c269d160b5f5a"),
    "ru": ("cyrillic", 5, "90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711"),
    "ar": ("arabic", 5, "c1192e632d0baa9146ae5b756a0e635e3dc63c1733737ebfd1629e87144e9295"),
}


def language_hint(text):
    for code, pattern in (
        ("ja", r"[\u3040-\u30ff]"),
        ("ko", r"[\uac00-\ud7a3]"),
        ("ru", r"[\u0400-\u04ff]"),
        ("ar", r"[\u0600-\u06ff]"),
    ):
        if re.search(pattern, text):
            return code
    return ""


@lru_cache(maxsize=3)
def ocr(lang=""):
    from rapidocr_onnxruntime import RapidOCR

    options = {"intra_op_num_threads": 2, "inter_op_num_threads": 1, "print_verbose": False}
    if os.environ.get("HANHUA_OCR_MODEL"):
        options["rec_model_path"] = os.environ["HANHUA_OCR_MODEL"]
    else:
        tag = (os.environ.get("HANHUA_OCR_LANGUAGE") or lang or "").lower().split("-")[0]
        tag = {"jpn": "ja", "jp": "ja", "kor": "ko", "eng": "en", "rus": "ru", "ara": "ar"}.get(tag, tag)
        if tag in ("fr", "fra", "es", "spa", "de", "deu", "it", "ita", "pt", "por", "vi", "tr", "pl"):
            tag = "latin"
        if tag in RECOGNIZERS:
            group, v, sha = RECOGNIZERS[tag]
            filename = f"{group}_PP-OCRv{v}_rec_mobile.onnx"
            url = f"https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx/PP-OCRv{v}/rec/{filename}"
            options["rec_model_path"] = str(download(url, MODELS / "ocr" / filename, sha))
    if os.environ.get("HANHUA_OCR_KEYS"):
        options["rec_keys_path"] = os.environ["HANHUA_OCR_KEYS"]
    return RapidOCR(**options)


def recognize(bgr, lang=""):
    rows, _ = ocr(lang)(bgr)
    result = []
    for quad, text, score in rows or []:
        if textlike(text) and float(score) >= 0.57:
            result.append(
                dict(
                    text=text.strip(), box=np.rint(quad).astype(int).tolist(), score=round(float(score), 3)
                )
            )
    return result


@lru_cache(maxsize=1)
def font_path():
    return download(
        "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf",
        MODELS / "NotoSansSC.ttf",
        "a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da",
    )


@lru_cache(maxsize=1)
def font_license():
    return download(
        "https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/OFL.txt",
        MODELS / "NotoSansSC-OFL.txt",
        "1c05c68c34f9708415aada51f17e1b0092d2cea709bf4a94cd38114f9e73d7d9",
    )


def paint(source, rows, trans, dest):
    im = Image.open(source).convert("RGBA")
    rgb = np.array(im.convert("RGB"))
    mask = np.zeros(rgb.shape[:2], np.uint8)
    for row in rows:
        cv2.fillPoly(mask, [np.asarray(row["box"], np.int32)], 255)
    mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
    clean = cv2.inpaint(rgb, mask, 3, cv2.INPAINT_TELEA)
    canvas = Image.fromarray(clean)
    draw = ImageDraw.Draw(canvas)
    for row in rows:
        box = np.array(row["box"])
        x, y = np.maximum(0, box.min(axis=0))
        right, bottom = np.minimum(im.size, box.max(axis=0) + 1)
        w, h = int(right - x), int(bottom - y)
        if w < 3 or h < 3:
            raise ValueError("Detected text region is too small")
        text = trans[row["id"]]
        for size in range(min(160, h), 3, -1):
            font = ImageFont.truetype(str(font_path()), size)
            lines = [""]
            for char in text:
                if char == "\n" or (lines[-1] and draw.textlength(lines[-1] + char, font=font) > w - 2):
                    lines.append("" if char == "\n" else char)
                else:
                    lines[-1] += char
            phrase = "\n".join(lines)
            bounds = draw.multiline_textbbox((0, 0), phrase, font=font, spacing=0)
            if bounds[2] - bounds[0] <= w and bounds[3] - bounds[1] <= h:
                break
        else:
            raise ValueError("Chinese text does not fit its image region")
        light = float(clean[int(y) : int(bottom), int(x) : int(right)].mean()) < 130
        draw.multiline_text(
            (
                int(x) + (w - bounds[2] + bounds[0]) / 2 - bounds[0],
                int(y) + (h - bounds[3] + bounds[1]) / 2 - bounds[1],
            ),
            phrase,
            font=font,
            fill="white" if light else "black",
            spacing=0,
            align="center",
        )
    canvas.putalpha(im.getchannel("A"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix.lower() in (".jpg", ".jpeg"):
        canvas.convert("RGB").save(dest, quality=95)
    else:
        canvas.save(dest)
    with Image.open(dest) as check:
        if check.size != im.size:
            raise ValueError("Image dimensions changed")


def norm(s):
    return re.sub(r"[\W_]+", "", s.casefold())


def alike(a, b):
    a, b = norm(a), norm(b)
    if a == b:
        return True
    if len(a) > len(b):
        a, b = b, a
    if len(a) < 4:
        return False
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    if matcher.ratio() > 0.78:
        return True
    if len(a) < 9:
        return False
    part = matcher.find_longest_match()
    start = max(0, min(len(b) - len(a), part.b - part.a))
    return SequenceMatcher(None, a, b[start : start + len(a)], autojunk=False).ratio() > 0.76


def screen_cues(path, info, bitmap=None, step=0.75, lang="", checkpoint=None):
    saved = read(checkpoint) if checkpoint and checkpoint.is_file() else {}
    if saved.get("finished"):
        return saved["cues"]
    video = next(
        s
        for s in info["streams"]
        if s["codec_type"] == "video" and not s.get("disposition", {}).get("attached_pic")
    )
    w = min(960, video["width"])
    h = max(2, round(video["height"] * w / video["width"] / 2) * 2)
    stamp = saved.get("stamp", 0)
    args = ["ffmpeg", "-nostdin", "-loglevel", "error"]
    if stamp:
        args += ["-ss", str(stamp)]
    args += ["-i", str(path)]
    if bitmap is None:
        args += ["-map", f"0:{video['index']}", "-vf", f"fps={1 / step},scale={w}:{h}"]
    else:
        args += [
            "-filter_complex",
            f"[0:{video['index']}]drawbox=c=black:t=fill[bg];[bg][0:{bitmap}]overlay,fps={1 / step},scale={w}:{h}[sub]",
            "-map",
            "[sub]",
        ]
    args += ["-an", "-sn", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"]
    current = saved.get("current", {})
    complete = saved.get("complete", [])
    last_save = stamp
    size = w * h * 3
    last_mini = last_rows = None
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as p:
        while True:
            raw = p.stdout.read(size)
            if len(raw) != size:
                break
            frame = np.frombuffer(raw, np.uint8).reshape(h, w, 3)
            mini = cv2.resize(frame, (100, 60))
            if last_mini is None or np.abs(mini.astype("int16") - last_mini).mean() > 1.4:
                last_rows = recognize(frame, lang)
                last_mini = mini
            active = {}
            for row in last_rows or []:
                key = norm(row["text"])
                if not key or key in active:
                    continue
                prior = current.pop(key, None)
                band = np.mean(np.asarray(row["box"])[:, 1]) / h > 0.61
                active[key] = prior or dict(
                    start=round(stamp * 1000),
                    text=row["text"],
                    speaker="",
                    kind="screen",
                    caption_band=bool(band),
                )
                active[key]["end"] = round((stamp + step) * 1000)
            complete.extend(current.values())
            current = active
            stamp += step
            if checkpoint and stamp - last_save >= 30:
                put(checkpoint, dict(stamp=stamp, current=current, complete=complete))
                last_save = stamp
        if p.wait() != 0:
            raise RuntimeError("Could not decode frames for OCR")
    complete.extend(current.values())
    cues = sorted(complete, key=lambda c: (c["start"], c["end"]))
    if checkpoint:
        put(checkpoint, dict(finished=True, cues=cues))
    return cues
