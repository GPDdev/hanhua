import hashlib
import os
import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf

from core import MODELS, download, ff, put, run, tree_extract
from filelock import FileLock

RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
TINY = {
    "config.json": "a73a28cdfe1c43ccc7202fa333d1f89c202477271407ae9a7f19afa52039cac8",
    "model.bin": "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1",
    "tokenizer.json": "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab",
    "vocabulary.txt": "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913",
}
SMALL = dict(
    TINY,
    **{
        "config.json": "b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828",
        "model.bin": "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
    },
)


@lru_cache(maxsize=2)
def whisper(size):
    from faster_whisper import WhisperModel

    model = size
    if size in ("tiny", "small"):
        model = MODELS / ("whisper-" + size)
        revision = "536b0662742c02347bc0e980a01041f333bce120" if size == "small" else "main"
        for file, sha in (SMALL if size == "small" else TINY).items():
            download(
                f"https://huggingface.co/Systran/faster-whisper-{size}/resolve/{revision}/{file}",
                model / file,
                sha,
            )
    return WhisperModel(
        str(model),
        device="cpu",
        compute_type="int8",
        cpu_threads=min(6, os.cpu_count() or 2),
        download_root=str(MODELS / "whisper"),
    )


def hear(audio, live=False, language=None):
    import psutil

    default = (
        "small"
        if not live or (psutil.virtual_memory().total >= 16 * 1024**3 and (os.cpu_count() or 1) >= 8)
        else "tiny"
    )
    model = whisper(os.environ.get("HANHUA_LIVE_WHISPER" if live else "HANHUA_WHISPER", default))
    lang = (language or "").lower().split("-")[0]
    lang = {
        "eng": "en",
        "jpn": "ja",
        "kor": "ko",
        "rus": "ru",
        "deu": "de",
        "fra": "fr",
        "spa": "es",
        "zho": "zh",
        "chi": "zh",
        "cmn": "zh",
        "yue": "yue",
    }.get(lang, lang)
    lang = lang if lang in model.supported_languages else None
    segments, info = model.transcribe(
        str(audio) if isinstance(audio, Path) else audio,
        vad_filter=True,
        language=lang,
        beam_size=1 if live else 3,
        condition_on_previous_text=not live,
    )
    return [
        dict(
            start=round(s.start * 1000),
            end=round(s.end * 1000),
            text=s.text.strip(),
            speaker="",
            kind="dialogue",
        )
        for s in segments
        if s.text.strip() and s.end > s.start
    ], info.language


def model_pack(tag, name, archive, marker, sha):
    directory = MODELS / name
    if not (directory / marker).is_file():
        package = download(RELEASE + tag + "/" + name + ".tar.bz2", MODELS / archive, sha)
        with FileLock(str(package) + ".unpack.lock"):
            if not (directory / marker).is_file():
                tree_extract(package, MODELS)
    return directory


def assign_speakers(wav, cues):
    import sherpa_onnx as so

    if not cues or all(c["speaker"] for c in cues):
        return
    folder = model_pack(
        "speaker-segmentation-models",
        "sherpa-onnx-pyannote-segmentation-3-0",
        "segmentation.tar.bz2",
        "model.onnx",
        "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
    )
    embedding = download(
        RELEASE + "speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx",
        MODELS / "speaker-embedding.onnx",
        "53cab20ba8f2eb2f6e2d89ecc18e88ef3222169b61b4419cbc13666744a4574b",
    )
    cfg = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(folder / "model.onnx")),
            num_threads=2,
        ),
        embedding=so.SpeakerEmbeddingExtractorConfig(model=str(embedding), num_threads=2),
        clustering=so.FastClusteringConfig(threshold=0.55),
    )
    diar = so.OfflineSpeakerDiarization(cfg)
    with sf.SoundFile(str(wav)) as f:
        # Bound peak memory on feature-length audio. Match local speaker clusters by voice embedding.
        extractor = so.SpeakerEmbeddingExtractor(cfg.embedding)
        profiles = []
        offset = 0
        for samples in f.blocks(blocksize=16000 * 300, dtype="float32"):
            groups = diar.process(samples).sort_by_start_time()
            labels = {}
            for label in {g.speaker for g in groups}:
                best = max((g for g in groups if g.speaker == label), key=lambda g: g.end - g.start)
                audio = samples[int(best.start * 16000) : int(min(best.end, best.start + 15) * 16000)]
                stream = extractor.create_stream()
                stream.accept_waveform(16000, audio)
                stream.input_finished()
                emb = None
                if extractor.is_ready(stream):
                    emb = np.array(extractor.compute(stream))
                    emb /= max(np.linalg.norm(emb), 1e-8)
                scores = [
                    float(np.dot(v, emb))
                    if v is not None and emb is not None and i not in labels.values()
                    else -1
                    for i, v in enumerate(profiles)
                ]
                if scores and max(scores) > 0.55:
                    idx = int(np.argmax(scores))
                else:
                    idx = len(profiles)
                    profiles.append(emb)
                labels[label] = idx
            for c in cues:
                if c.get("speaker") or c["end"] <= offset or c["start"] >= offset + len(samples) / 16:
                    continue
                overlaps = [
                    (
                        min(c["end"], offset + g.end * 1000) - max(c["start"], offset + g.start * 1000),
                        g.speaker,
                    )
                    for g in groups
                ]
                if overlaps and max(overlaps)[0] > 0:
                    c["speaker"] = "speaker-" + str(labels[max(overlaps)[1]] + 1)
            offset += round(len(samples) / 16)


def speak(jobs, work, lang="zh"):
    if not jobs:
        return
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    todo = [dict(text=t, voice=v, lang=lang, path=str(Path(p).absolute())) for t, v, p in jobs]
    for row in todo:
        Path(row["path"]).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as scratch:
        spec = Path(scratch) / "requests.json"
        staged = [dict(row, path=str(Path(scratch) / f"{i}.wav")) for i, row in enumerate(todo)]
        put(spec, staged)
        try:
            run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    Path(__file__).with_name("speak.ps1"),
                    spec,
                ]
            )
        except RuntimeError:
            kokoro(staged)
        for row, original in zip(staged, todo):
            if sf.info(row["path"]).frames <= 0:
                raise RuntimeError("Voice synthesis returned empty audio")
            os.replace(row["path"], original["path"])


def kokoro(rows):
    import sherpa_onnx as so

    name = "kokoro-int8-multi-lang-v1_1"
    folder = model_pack(
        "tts-models",
        name,
        name + ".tar.bz2",
        "model.int8.onnx",
        "259c9c8bc906d6078e4e0570421885dabb68b539c2f970fcbb890a58eeaf3e9c",
    )
    config = so.OfflineTtsConfig()
    config.model.kokoro.model = str(next(folder.glob("*.onnx")))
    config.model.kokoro.voices = str(folder / "voices.bin")
    config.model.kokoro.tokens = str(folder / "tokens.txt")
    config.model.kokoro.data_dir = str(folder / "espeak-ng-data")
    config.model.kokoro.lexicon = ",".join(str(folder / p) for p in ("lexicon-zh.txt", "lexicon-us-en.txt"))
    config.model.kokoro.dict_dir = str(folder / "dict")
    config.model.num_threads = 3
    tts = so.OfflineTts(config)
    for r in rows:
        # Upstream orders the three English voices first, followed by the Chinese voices.
        voice = (3 + int(r["voice"]) % 100) if r["lang"] == "zh" else int(r["voice"]) % 3
        a = tts.generate(r["text"], sid=voice, speed=1)
        sf.write(r["path"], a.samples, a.sample_rate)


def atempo(ratio):
    parts = []
    while ratio > 2:
        parts.append("atempo=2")
        ratio /= 2
    parts.append(f"atempo={max(0.5, ratio):.6f}")
    return ",".join(parts)


def voice_track(cues, trans, cache, duration, dest, roles=None):
    cache.mkdir(parents=True, exist_ok=True)
    if roles is None:
        roles = {}
    new = {}
    utterances = []
    for c in cues:
        if c["kind"] != "dialogue":
            continue
        role = c["speaker"] or "narrator"
        idx = roles.setdefault(role, len(roles))
        zh = trans[c["id"]]
        key = hashlib.sha256(f"{idx}|{zh}".encode()).hexdigest()
        file = cache / (key + ".wav")
        if not file.is_file():
            new[key] = (zh, idx, file)
        utterances.append((c, file))
    speak(list(new.values()), cache)
    rate = 24000
    # Disk backing keeps hour-long tracks out of the Python heap.
    with tempfile.TemporaryDirectory(dir=cache) as temp:
        track = np.memmap(
            Path(temp) / "voice.pcm", mode="w+", dtype=np.float32, shape=(max(1, int(duration * rate)),)
        )
        for c, wav in utterances:
            window = max(0.08, (c["end"] - c["start"]) / 1000)
            ratio = max(1, sf.info(str(wav)).duration / window)
            data = np.frombuffer(
                ff("-i", wav, "-af", atempo(ratio), "-ar", rate, "-ac", 1, "-f", "f32le", "pipe:1"),
                dtype="<f4",
            )
            a = max(0, round(c["start"] * rate / 1000))
            n = min(len(data), len(track) - a, round(window * rate))
            if n > 0:
                track[a : a + n] += data[:n]
        with sf.SoundFile(str(dest), "w", rate, 1, subtype="PCM_16") as f:
            for k in range(0, len(track), rate * 60):
                f.write(np.clip(track[k : k + rate * 60], -0.98, 0.98))
        del track
    return roles


def separate(source, dest):
    import sherpa_onnx as so

    folder = model_pack(
        "source-separation-models",
        "sherpa-onnx-spleeter-2stems-int8",
        "spleeter-int8.tar.bz2",
        "vocals.int8.onnx",
        "c5dc8edfa08cc9a6d82670fcca15cfe671e446385e1a28c9da8c1816d238ea17",
    )
    model = so.OfflineSourceSeparationModelConfig(
        spleeter=so.OfflineSourceSeparationSpleeterModelConfig(
            vocals=str(folder / "vocals.int8.onnx"), accompaniment=str(folder / "accompaniment.int8.onnx")
        ),
        num_threads=3,
    )
    sep = so.OfflineSourceSeparation(so.OfflineSourceSeparationConfig(model=model))
    with sf.SoundFile(str(source)) as f, sf.SoundFile(str(dest), "w", 44100, 2, subtype="PCM_16") as out:
        if f.samplerate != 44100 or f.channels != 2:
            raise ValueError("Separation expects stereo 44.1 kHz")
        # Pad both sides, then trim to the input timeline to avoid block edges and drift.
        block = 44100 * 24
        margin = 44100
        for pos in range(0, len(f), block):
            start = max(0, pos - margin)
            f.seek(start)
            audio = f.read(
                min(len(f) - start, pos + block + margin - start), dtype="float32", always_2d=True
            )
            # Spleeter rounds the number of output samples down to a transform hop.
            # Right padding keeps the actual last samples inside the model output.
            padded = np.pad(audio.T, ((0, 0), (0, 4096)))
            result = sep.process(44100, np.ascontiguousarray(padded))
            backing = np.asarray(result.stems[1].data, dtype=np.float32)
            if backing.ndim != 2 or not np.isfinite(backing).all():
                raise RuntimeError("Invalid separated audio")
            clipped = backing.T[pos - start : pos - start + min(block, len(f) - pos)]
            if len(clipped) != min(block, len(f) - pos):
                raise ValueError("Separation output has wrong length")
            out.write(clipped)
