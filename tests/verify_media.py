"""Probe and decode the acceptance episodes. The reference WAV contains four utterances."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pysubs2
from core import HOME, ff, probe, put, read


def packet_hash(path, selector):
    return (
        ff("-i", path, "-map", selector, "-c", "copy", "-f", "hash", "-hash", "sha256", "pipe:1")
        .decode()
        .strip()
    )


def main():
    base = HOME / "verification"
    outputs = base / "movie-batch"
    checks = []
    for source in sorted((base / "cases").glob("*.mkv")):
        report = read(outputs / source.name / "report.json")
        result = Path(report["result"])
        ff("-i", result, "-map", "0:v:0", "-map", "0:a", "-xerror", "-f", "null", "-")
        streams = probe(result)["streams"]
        aud = [s for s in streams if s["codec_type"] == "audio"]
        subs = [s for s in streams if s["codec_type"] == "subtitle"]
        defaults = [s.get("tags", {}).get("title") for s in subs if s["disposition"]["default"]]
        cues = pysubs2.load(report["subtitle"], encoding="utf-8-sig")
        assert len(aud) == 2 and aud[1]["disposition"]["default"]
        assert defaults == ["中外双语"], defaults
        assert report["screen"] == 1 and report["dialogue"] == 4, (source, report)
        assert len(cues) == 5 and all(c.end > c.start for c in cues)
        assert packet_hash(source, "0:v:0") == packet_hash(result, "0:v:0")
        assert packet_hash(source, "0:a:0") == packet_hash(result, "0:a:0")
        diff = abs(float(probe(source)["format"]["duration"]) - float(probe(result)["format"]["duration"]))
        assert diff < 0.12
        checks.append(
            dict(
                input=source.name,
                mode=report["audio_mode"],
                source_kind=report["dialogue_source"],
                roles=report["voices"],
                delta_seconds=round(diff, 4),
                decoded=True,
                original_streams_match=True,
            )
        )
    put(base / "media-acceptance.json", checks)
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
