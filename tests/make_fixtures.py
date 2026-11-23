"""Build reproducible, separate acceptance inputs under the local verification cache."""

import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from PIL import Image, ImageDraw, ImageFont
from core import HOME, download, ff, run
from game import renpy_sdk


def make():
    base = HOME / "verification"
    base.mkdir(parents=True, exist_ok=True)
    media = base / "cases"
    media.mkdir(exist_ok=True)
    audio = download(
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/1-two-speakers-en.wav",
        base / "two-speakers.wav",
    )
    art = Image.new("RGB", (720, 400), (37, 44, 58))
    draw = ImageDraw.Draw(art)
    face = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 32)
    draw.text((40, 35), "NORTH GATE", font=face, fill="white")
    art.save(base / "sign.png")
    subtitles = base / "reference.srt"
    subtitles.write_text(
        "1\n00:00:01,300 --> 00:00:04,080\nA pencil with black lead writes best.\n\n2\n00:00:04,350 --> 00:00:08,950\nThe lamp shone with a steady green flame.\n\n3\n00:00:09,200 --> 00:00:11,900\nClothes and lodging are free to new men.\n\n4\n00:00:12,080 --> 00:00:15,550\nThe glow deepened in the eyes of the sweet girl.\n",
        encoding="utf-8",
    )
    clean = media / "AudioOnly.mkv"
    if not clean.is_file():
        ff(
            "-loop",
            "1",
            "-framerate",
            "12",
            "-i",
            base / "sign.png",
            "-i",
            audio,
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=196:sample_rate=44100:duration=16",
            "-filter_complex",
            "[2:a]volume=0.18[bg];[1:a][bg]amix=inputs=2:normalize=0:duration=longest[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-t",
            "16",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "language=eng",
            clean,
        )
    soft = media / "Soft.mkv"
    if not soft.is_file():
        ff(
            "-i",
            clean,
            "-i",
            subtitles,
            "-map",
            "0",
            "-map",
            "1:s:0",
            "-c",
            "copy",
            "-metadata:s:s:0",
            "language=eng",
            soft,
        )
    hard = media / "Hard.mkv"
    if not hard.is_file():
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                clean,
                "-vf",
                "subtitles=reference.srt:force_style='Fontname=Arial,Fontsize=22,Outline=2,MarginV=15'",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-c:a",
                "copy",
                hard,
            ],
            cwd=base,
        )
    sample = base / "TheQuestion"
    if not sample.exists():
        shutil.copytree(
            renpy_sdk().parent / "the_question" / "game",
            sample / "game",
            ignore=shutil.ignore_patterns("cache", "saves"),
        )
        # Exercise new translations as well as existing localization in the official game.
        tl = sample / "game" / "tl" / "schinese" / "script.rpy"
        text = tl.read_text(encoding="utf-8-sig")
        text = re.sub(r'(?m)^    "[\u3400-\u9fff][^"\n]*"$', '    ""', text, count=2)
        tl.write_text(text, encoding="utf-8")
        tl.with_suffix(".rpyc").unlink(missing_ok=True)
        (sample / "game" / "images" / "entrance.png").write_bytes((base / "sign.png").read_bytes())
        destination = sample / "game" / "voice" / "guide" / "line.wav"
        destination.parent.mkdir(parents=True, exist_ok=True)
        ff("-i", audio, "-ss", "1.2", "-t", "3.1", destination)
    odd = base / "FieldJournal"
    (odd / "i18n").mkdir(parents=True, exist_ok=True)
    (odd / "i18n" / "journal.xlf").write_text(
        '<xliff xmlns="urn:oasis:names:tc:xliff:document:1.2" version="1.2"><file source-language="en" target-language="zh-CN" datatype="plaintext" original="journal"><body>'
        '<trans-unit id="gate"><source>Open the north gate</source><target></target></trans-unit>'
        '<trans-unit id="keys"><source>You found {count} keys.</source><target></target></trans-unit>'
        "</body></file></xliff>",
        encoding="utf-8",
    )
    (odd / "read_journal.py").write_text(
        'from pathlib import Path\nimport xml.etree.ElementTree as ET\np=Path(__file__).parent/"i18n"/"journal.xlf"\nfor u in ET.parse(p).findall(".//{*}trans-unit"):\n    print((u.findtext("{*}target") or u.findtext("{*}source")).format(count=2))\n',
        encoding="utf-8",
    )
    print(sample, media, odd, sep="\n")


if __name__ == "__main__":
    make()
