import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock
from core import (
    HOME,
    VIDEO,
    SCHEMA_VERSION,
    Ollama,
    atomic,
    db_open,
    digest,
    glossary,
    put,
    read,
    store_batch,
    translate_pending,
    translations,
    validate,
)


def inventory(source):
    if source.is_file():
        if source.suffix.lower() not in VIDEO:
            raise ValueError("Expected a video file or a game directory")
        return "movie", [source]
    if not source.is_dir():
        raise FileNotFoundError(str(source))
    direct = list(source.iterdir())
    markers = {"game", "www", "project.godot", "content", "data", "localization", "lang", "locales"}
    if any(
        p.name.lower() in markers or p.suffix.lower() in {".exe", ".assets", ".locres", ".ab", ".pck"}
        for p in direct
    ):
        return "game", [source]
    vids = sorted(
        p
        for p in source.rglob("*")
        if p.is_file()
        and p.suffix.lower() in VIDEO
        and not any(x.endswith(".hanhua") for x in p.relative_to(source).parts)
    )
    return ("movie", vids) if vids else ("game", [source])


def signature(source, kind, adapter):
    import game, movie

    h = hashlib.sha256(str(SCHEMA_VERSION).encode())
    paths = [source, *movie.sidecars(source)] if kind == "movie" else sorted(game.files(source))
    for p in paths:
        if kind == "game" and (
            p.suffix.lower() in (".log", ".rpyb") or p.name in ("log.txt", "traceback.txt", "errors.txt")
        ):
            continue
        stat = p.stat()
        h.update(f"{p}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    if adapter:
        h.update(digest(adapter).encode())
    h.update(str(os.environ.get("HANHUA_WHISPER", "small")).encode())
    for key in ("HANHUA_OCR_LANGUAGE", "HANHUA_OCR_MODEL", "HANHUA_OCR_KEYS"):
        h.update(os.environ.get(key, "").encode())
    return h.hexdigest()


def layout(args):
    source = Path(args.input).resolve(strict=True)
    kind, inputs = inventory(source)
    if args.out:
        base = Path(args.out).resolve()
    else:
        base = source.with_name(source.name + ".hanhua")
        try:
            base.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=base):
                pass
        except PermissionError:
            base = (
                HOME
                / "outputs"
                / (source.name + "-" + hashlib.sha256(str(source).encode()).hexdigest()[:8])
            )
    if base == source or (source.is_dir() and base.is_relative_to(source)):
        raise ValueError("Choose an output directory outside the input directory")
    base.mkdir(parents=True, exist_ok=True)
    tasks = []
    for inp in inputs:
        out = base if inp == source else base / inp.relative_to(source)
        out.mkdir(parents=True, exist_ok=True)
        if inp != source:
            marker = out / ".series.json"
            if not marker.is_file():
                put(marker, {"base": str(base), "source": str(inp)})
        tasks.append((kind, inp, out))
    return base, tasks


def get_plan(kind, source, out, args):
    import game, movie

    adapter = getattr(args, "adapter", None)
    workroot = out / ".work"
    workroot.mkdir(exist_ok=True)
    index = workroot / "current.json"
    if not adapter and index.is_file():
        prior = read(index).get("adapter")
        if prior:
            adapter = Path(prior)
    sig = signature(source, kind, adapter)
    work = workroot / sig[:16]
    work.mkdir(exist_ok=True)
    manifest = work / "plan.json"
    if not manifest.exists() or getattr(args, "refresh", False):
        if getattr(args, "refresh", False) and kind == "movie":
            for checkpoint in [*work.glob("ocr-*.json"), work / "speech-cues.json"]:
                checkpoint.unlink(missing_ok=True)
        plan = movie.prepare(source, work) if kind == "movie" else game.prepare(source, work, adapter)
        plan["units"] = list({u["id"]: u for u in plan["units"]}.values())
        plan.update(source=str(source), fingerprint=sig)
        put(manifest, plan)
    else:
        plan = read(manifest)
    put(index, dict(fingerprint=sig, adapter=str(Path(adapter).resolve()) if adapter else None))
    return plan, work


def live_launcher(out, source):
    launcher = Path(__file__).resolve().parent.parent / "hanhua.ps1"
    # Use a PowerShell file with single-quoted literals so user directory names remain data.
    quote = lambda p: "'" + str(p).replace("'", "''") + "'"
    script = (
        "& "
        + quote(launcher)
        + " live --game "
        + quote(source)
        + " --out "
        + quote(out)
        + " --wait 180\nexit $LASTEXITCODE\n"
    )
    atomic(out / "live.ps1", script.encode("utf-8-sig"))
    atomic(
        out / "live.cmd",
        b'@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0live.ps1"\r\n',
    )


@contextmanager
def episode_result(errors, source, out, series):
    try:
        yield
    except Exception as e:
        if not series:
            raise
        error = {"source": str(source), "output": str(out), "error": str(e)}
        put(out / "error.json", error)
        errors.append(error)


def jobs(args):
    import game, movie

    base, tasks = layout(args)
    series = len(tasks) > 1
    if len(tasks) == 1 and (base / ".series.json").is_file():
        marker = read(base / ".series.json")
        parent = Path(marker["base"]).resolve()
        if tasks[0][1] == Path(marker["source"]) and base.is_relative_to(parent):
            base = parent
    db = db_open(base)
    engine = None
    summaries = []
    pending = []
    all_units = []
    errors = []
    try:
        for kind, source, out in tasks:
            with (
                episode_result(errors, source, out, series),
                FileLock(str(out / ".job.lock"), timeout=3600),
            ):
                plan, work = get_plan(kind, source, out, args)
                units = plan["units"]
                all_units.extend(units)
                # Existing target subtitles can reflow lines or tags; validate before reuse.
                known = translations(db)
                for u in units:
                    if u["id"] in known:
                        try:
                            validate(u, known[u["id"]])
                        except ValueError:
                            with db:
                                db.execute(
                                    "DELETE FROM translations WHERE id=? AND zh=?",
                                    (u["id"], known[u["id"]]),
                                )
                            known.pop(u["id"])
                for u in units:
                    if u.get("known") and u["id"] not in known:
                        try:
                            store_batch(db, [u], {u["id"]: u["known"]})
                        except ValueError:
                            pass
                known = translations(db)
                missing = [u for u in units if u["id"] not in known]
                item = dict(
                    source=str(source), kind=kind, units=len(units), pending=len(missing), output=str(out)
                )
                if kind == "game":
                    item.update(
                        engines=plan["engines"],
                        resource_types=sorted({a["type"] for a in plan["assets"]}),
                        unhandled=plan["unsupported"][:30],
                        notes=plan["notes"][:12],
                    )
                pending.extend(missing)
                if args.command in ("run", "build"):
                    if missing and args.command == "run":
                        engine = engine or Ollama()
                        translate_pending(db, units, base, engine)
                    elif missing:
                        raise ValueError(
                            f"{len(missing)} translations still needed for {source.name}. Use next/submit or run."
                        )
                    known = translations(db)
                    state = hashlib.sha256(
                        json.dumps([(u["id"], known[u["id"]]) for u in units], ensure_ascii=False).encode()
                    ).hexdigest()
                    report = out / "report.json"
                    previous = read(report) if report.is_file() else {}
                    previous_path = previous.get("result") if kind == "movie" else previous.get("patch")
                    reusable = (
                        previous.get("fingerprint") == plan["fingerprint"]
                        and previous.get("translation_hash") == state
                        and bool(previous_path)
                        and Path(previous_path).is_file()
                    )
                    if reusable and kind == "movie":
                        reusable = all(Path(previous[key]).is_file() for key in ("subtitle", "bilingual"))
                    if reusable and not getattr(args, "refresh", False):
                        result = previous
                    else:
                        result = (movie.build if kind == "movie" else game.build)(
                            source, plan, known, out, work
                        )
                        if kind == "game":
                            live_launcher(out, source)
                            result["live_launcher"] = str(out / "live.cmd")
                        result["retained_source_text"] = [
                            {"source": u["source"], "context": u["context"]}
                            for u in units
                            if known[u["id"]] == u["source"]
                            and u["source"].isascii()
                            and any(c.isalpha() for c in u["source"])
                        ]
                        result.update(
                            source=str(source), fingerprint=plan["fingerprint"], translation_hash=state
                        )
                        put(report, result)
                    item.update(
                        report=str(report), result=result.get("result") or result.get("patch"), pending=0
                    )
                summaries.append(item)
                if args.command in ("run", "build"):
                    (out / "error.json").unlink(missing_ok=True)
        if args.command == "next":
            ids = set()
            batch = []
            for u in pending:
                if u["id"] in ids:
                    continue
                ids.add(u["id"])
                batch.append(u)
                if len(batch) >= args.limit:
                    break
            result = dict(glossary=glossary(base), units=batch, errors=errors)
            if args.file:
                put(args.file, result)
                return {"file": str(args.file), "count": len(batch)}
            return result
        if args.command == "submit":
            store_batch(db, all_units, read(args.file))
            return {"saved": len(read(args.file))}
        return {"jobs": summaries, "errors": errors, "glossary": str(base / "glossary.json")}
    finally:
        glossary(base)
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Windows 游戏与影视汉化")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "prepare", "next", "submit", "build"):
        p = sub.add_parser(name)
        p.add_argument("input")
        p.add_argument("--out")
        p.add_argument("--adapter", type=Path)
        if name in ("run", "prepare", "build"):
            p.add_argument("--refresh", action="store_true")
        if name == "next":
            p.add_argument("--limit", type=int, default=40)
            p.add_argument("--file", type=Path)
        if name == "submit":
            p.add_argument("--file", type=Path, required=True)
    for name in ("apply", "restore"):
        p = sub.add_parser(name)
        p.add_argument("manifest")
        p.add_argument("--game")
    p = sub.add_parser("live")
    p.add_argument("--list", action="store_true")
    p.add_argument("--game")
    p.add_argument("--pid", type=int)
    p.add_argument("--window")
    p.add_argument("--lang", default="")
    p.add_argument("--no-audio", action="store_true")
    p.add_argument("--out")
    p.add_argument("--seconds", type=float)
    p.add_argument("--wait", type=int, default=0)
    args = parser.parse_args()
    if args.command in ("apply", "restore"):
        from patcher import perform

        result = perform(args.command, args.manifest, args.game)
    elif args.command == "live":
        import live

        if args.wait and not args.list:
            deadline = time.monotonic() + args.wait
            while True:
                try:
                    live.target(args)
                    break
                except ValueError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(1)
        result = live.start(args)
    else:
        result = jobs(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get("errors"):
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        if os.environ.get("HANHUA_DEBUG"):
            raise
        print(str(e), file=sys.stderr)
        sys.exit(1)
