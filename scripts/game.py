import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from core import HOME, TOOLS, digest, download, ff, put, run, tree_extract, unit
from formats import collect, rewrite, entry
from patcher import below, rpyc_tag
from sound import hear
from vision import font_license, font_path, language_hint, paint, recognize
import locales

VOICE_EXT = {".ogg", ".opus", ".wav", ".mp3", ".m4a", ".flac"}
IMAGE_EXT = {".png", ".webp", ".jpg", ".jpeg"}
IGNORE = {
    ".git",
    ".hanhua-backup",
    "node_modules",
    "saves",
    "__pycache__",
    ".godot",
    "cache",
    "renpy",
    "python-packages",
    "lib",
}


def files(root):
    for parent, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [
            s
            for s in dirs
            if s.lower() not in IGNORE and not s.endswith(".hanhua") and not (Path(parent) / s).is_symlink()
        ]
        for n in names:
            p = Path(parent) / n
            if p.is_file() and not p.is_symlink():
                yield p


def renpy_sdk():
    env = os.environ.get("HANHUA_RENPY_SDK")
    if env:
        p = Path(env)
        return p if p.is_file() else p / "renpy.exe"
    name = "renpy-8.5.3-sdk"
    for base in (TOOLS, HOME / "verification"):
        exe = base / name / "renpy.exe"
        if exe.is_file():
            return exe
    zipfile = download(
        "https://www.renpy.org/dl/8.5.3/renpy-8.5.3-sdk.zip",
        TOOLS / (name + ".zip"),
        "ff57648f9c04f27e381c48af6d8e3ee3cdec296bed4d3831f47f09b0a71b505e",
    )
    tree_extract(zipfile, TOOLS)
    return TOOLS / name / "renpy.exe"


def renpy_cmd(stage, *args):
    exe = renpy_sdk()
    env = dict(os.environ, RENPY_PATH_TO_SAVES=str(stage.parent / "renpy-saves"), SDL_AUDIODRIVER="dummy")
    # Console interpreter propagates errors and waits for completion on Windows.
    python = next(iter(exe.parent.glob("lib/py*-windows-x86_64/python.exe")), None)
    cmd = [python, exe.parent / "renpy.py"] if python else [exe]
    return run([*cmd, stage, *args], env=env, timeout=300)


def plugin(path):
    spec = importlib.util.spec_from_file_location("hanhua_game_adapter", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(root, work, adapter=None):
    units, assets, notes, unsupported = [], [], [], []
    anchors = {}
    inventory = list(files(root))
    skip_locales, pairs = locales.select(root, inventory)

    def rel(p):
        return p.relative_to(root).as_posix()

    def add(kind, source, target=None, template=None):
        rows = collect(kind, template or source, rel(source), units)
        if rows:
            if source in pairs and kind in ("json", "po", "locres"):
                template = locales.reuse(kind, source, pairs[source], work, rows, units)
                target = rel(pairs[source])
                anchors[rel(source)] = digest(source)
            assets.append(
                dict(
                    type=kind,
                    path=rel(source),
                    target=target or rel(source),
                    rows=rows,
                    template=str(template.relative_to(work)) if template else None,
                )
            )

    def anchor(p):
        if p.is_file():
            anchors[rel(p)] = digest(p)

    renpy = (root / "game").is_dir() and any(
        p.suffix.lower() in (".rpy", ".rpyc", ".rpa") for p in inventory
    )
    rpg = any(p.name == "System.json" and p.parent.name.lower() == "data" for p in inventory)
    godot = any(p.name == "project.godot" for p in inventory)
    projects = sorted(
        (p.parent for p in inventory if p.name == "project.godot"), key=lambda p: len(p.parts), reverse=True
    )
    engines = []
    if renpy:
        engines.append("Ren'Py")
        stage = work / "renpy-project"
        if stage.exists():
            if stage.is_symlink() or stage.resolve().parent != work.resolve():
                raise ValueError("Invalid staging directory")
            shutil.rmtree(stage)
        (stage / "game").mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            root / "game",
            stage / "game",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("saves", "cache"),
        )
        for n in ("script.rpy", "script.rpyc", "options.rpy", "archive.rpa"):
            anchor(root / "game" / n)
        try:
            renpy_cmd(stage, "translate", "schinese", "--empty", "--no-todo")
            for p in sorted((stage / "game" / "tl" / "schinese").rglob("*.rpy")):
                source = root / p.relative_to(stage)
                add("renpy", source, template=p)
            assets.append(
                dict(
                    type="renpy-language",
                    path="game/zz_hanhua_language.rpy",
                    target="game/zz_hanhua_language.rpy",
                    rows=[],
                )
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            notes.append("Ren'Py 生成翻译模板失败 " + str(e))
    if rpg:
        engines.append("RPG Maker MV/MZ")
    if godot:
        engines.append("Godot")
    if any(p.suffix == ".assets" or p.name == "UnityPlayer.dll" for p in inventory):
        engines.append("Unity")
    if any(p.suffix.lower() in (".locres", ".pak", ".utoc") for p in inventory):
        engines.append("Unreal")
    lang = None
    for p in sorted(inventory, key=lambda p: p.suffix.lower() in IMAGE_EXT):
        r = rel(p)
        s = p.suffix.lower()
        parts = [x.lower() for x in p.relative_to(root).parts]
        if p in skip_locales:
            continue
        if renpy and ("tl" in parts or not r.startswith("game/")):
            continue
        try:
            if (
                s == ".json"
                and rpg
                and p.parent.name.lower() == "data"
                and re.fullmatch(
                    r"Actors|Armors|Classes|CommonEvents|Enemies|Items|Map\d+|Skills|States|System|Troops|Weapons",
                    p.stem,
                )
            ):
                add("rpg", p)
                if p.stem == "System":
                    anchor(p)
            elif s == ".csv":
                add("csv", p)
            elif s == ".po":
                project = next((g for g in projects if p.is_relative_to(g)), None)
                target = None
                if project and p not in pairs:
                    translated = project / "hanhua_locale" / p.relative_to(project).with_suffix(".zh_CN.po")
                    target = rel(translated)
                add("po", p, target)
            elif s == ".locres":
                add("locres", p)
            elif s in (".assets", ".bundle", ".ab", ".unity3d") or p.name == "resources.assets":
                add("unity", p)
            elif s == ".json" and (
                set(parts[:-1])
                & {"locales", "localization", "lang", "languages", "translations", "i18n", "locale"}
            ):
                choices = {"en", "en_us", "en-us", "english", "zh", "zh-cn", "zh_cn", "zh-hans", "schinese"}
                if p.stem.lower() in choices and p.stem.lower() not in {"en", "en_us", "en-us", "english"}:
                    continue
                add("json", p)
            elif s in IMAGE_EXT and (not renpy or r.startswith("game/")):
                # Translate standalone image assets only. Preserve atlases and animation grids.
                if (
                    rpg
                    and set(parts)
                    & {"characters", "tilesets", "sv_actors", "sv_enemies", "animations", "faces", "icons"}
                ) or p.with_suffix(".atlas").is_file():
                    unsupported.append(r + " (图集)")
                    continue
                arr = np.asarray(Image.open(p).convert("RGB"))
                if min(arr.shape[:2]) < 12:
                    continue
                if lang is None:
                    lang = language_hint(" ".join(u["source"] for u in units[:1000]))
                found = recognize(cv2.cvtColor(arr, cv2.COLOR_RGB2BGR), lang)
                if found:
                    target = "game/tl/schinese/" + r[5:] if renpy else r
                    for row in found:
                        u = unit(row["text"], r, kind="image")
                        units.append(u)
                        row["id"] = u["id"]
                    assets.append(dict(type="image", path=r, target=target, rows=found))
            elif s in VOICE_EXT and (
                set(parts[:-1]) & {"voice", "voices", "dialog", "dialogue", "vo", "speech", "voiceover"}
                or re.match(r"(?:voice|dialog|speech)[_-]", p.stem, re.I)
            ):
                cues, _ = hear(p)
                if cues:
                    role = p.parent.name if p.parent.name.lower() not in {"voice", "voices", "vo"} else ""
                    for c in cues:
                        u = unit(c["text"], r, role, "voice")
                        units.append(u)
                        c["id"] = u["id"]
                    assets.append(
                        dict(
                            type="voice",
                            path=r,
                            target="game/tl/schinese/" + r[5:] if renpy else r,
                            rows=cues,
                        )
                    )
            elif s in (
                ".pak",
                ".utoc",
                ".pck",
                ".rpa",
                ".bank",
                ".rpgmvp",
                ".rpgmvo",
                ".xlf",
                ".xliff",
                ".xml",
                ".dat",
                ".mo",
                ".resources",
                ".yaml",
                ".yml",
                ".uasset",
                ".db",
            ):
                unsupported.append(r)
        except Exception as e:
            notes.append(r + " 转实时 " + str(e)[:400])
    if adapter:
        adapter = Path(adapter).resolve(strict=True)
        module = plugin(adapter)
        for spec in module.scan(root):
            src = below(root, spec["path"])
            rows = []
            for item in spec["entries"]:
                entry(
                    rows,
                    units,
                    item["loc"],
                    item["source"],
                    item.get("context", spec["path"]),
                    item.get("speaker", ""),
                )
            if rows:
                assets.append(
                    dict(type="custom", path=rel(src), target=spec.get("target", rel(src)), rows=rows)
                )
        adapter = {"path": str(adapter), "sha256": digest(adapter)}
    handled = {a["path"] for a in assets}
    if "Unity" not in engines and any(a["type"] == "unity" for a in assets):
        engines.append("Unity")
    unsupported = [p for p in unsupported if p not in handled]
    return dict(
        kind="game",
        engines=engines or ["未知或自定义格式"],
        assets=assets,
        units=units,
        anchors=anchors,
        unsupported=unsupported,
        notes=notes,
        adapter=adapter,
        renpy=renpy,
    )


def build(root, plan, trans, out, work):
    notes = list(plan["notes"])
    staged = Path(tempfile.mkdtemp(prefix="patch-", dir=work))
    payload = staged / "payload"
    payload.mkdir()
    coverage = []
    roles = {}
    custom = None
    if plan.get("adapter"):
        a = plan["adapter"]
        if digest(a["path"]) != a["sha256"]:
            raise ValueError("Adapter changed since inspection")
        custom = plugin(a["path"])
    for a in plan["assets"]:
        source = below(root, a["path"])
        dst = below(payload, a["target"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        typ = a["type"]
        rows = a["rows"]
        try:
            if typ == "renpy-language":
                if source.exists():
                    raise ValueError("Reserved activation file already exists")
                font = payload / "game" / "hanhua-font" / "NotoSansSC.ttf"
                font.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(font_path(), font)
                shutil.copy2(font_license(), font.with_name("OFL.txt"))
                shutil.copyfile(Path(__file__).parent / "templates" / "renpy_language.rpy", dst)
            elif typ == "image":
                paint(source, rows, trans, dst)
            elif typ == "voice":
                from sound import voice_track
                from core import probe

                text = [dict(r, kind="dialogue", speaker=(Path(a["path"]).parent.name)) for r in rows]
                base = work / "game-voice"
                base.mkdir(exist_ok=True)
                wav = base / "render.wav"
                duration = float(probe(source)["format"]["duration"])
                voice_track(text, trans, base / "cache", duration, wav, roles)
                ff("-i", wav, dst)
                if abs(float(probe(dst)["format"]["duration"]) - duration) > 0.25:
                    raise ValueError("Voice duration changed")
            elif typ == "custom":
                # Hand public-format adapters a copy and verify the no-op and translated round trips.
                with tempfile.TemporaryDirectory(dir=work) as temp:
                    cp = Path(temp) / source.name
                    shutil.copy2(source, cp)
                    original = [(r["loc"], custom.readback(cp, r["loc"])) for r in rows]
                    nop = Path(temp) / ("test" + source.suffix)
                    custom.render(cp, nop, original)
                    if not all(custom.readback(nop, k) == v for k, v in original):
                        raise ValueError("Adapter failed source round trip")
                    values = [(r["loc"], trans[r["id"]]) for r in rows]
                    custom.render(cp, dst, values)
                    if not all(custom.readback(dst, k) == v for k, v in values):
                        raise ValueError("Adapter failed translation round trip")
            else:
                template = work / a["template"] if a.get("template") else source
                rewrite(typ, template, dst, rows, trans)
            coverage.append(dict(path=a["target"], type=typ, count=len(rows)))
        except Exception as e:
            dst.unlink(missing_ok=True)
            notes.append(a["path"] + " 转实时 " + str(e)[:450])
    if "Godot" in plan["engines"]:
        verified = {a["path"] for a in coverage}
        source_assets = [a for a in plan["assets"] if a["target"] in verified]
        for project in (p for p in files(root) if p.name == "project.godot"):
            generated = locales.godot_files(project, root, payload, source_assets, trans)
            if generated:
                coverage.append(
                    dict(
                        path=project.relative_to(root).as_posix(), type="godot-locale", count=len(generated)
                    )
                )
    if plan["renpy"] and (payload / "game" / "zz_hanhua_language.rpy").exists():
        stage = work / "renpy-project"
        shutil.copytree(payload / "game", stage / "game", dirs_exist_ok=True)
        try:
            renpy_cmd(stage, "compile")
            for p in list(payload.rglob("*.rpy")):
                comp = (stage / p.relative_to(payload)).with_suffix(".rpyc")
                if comp.is_file():
                    shutil.copy2(comp, p.with_suffix(".rpyc"))
        except Exception as e:
            raise RuntimeError("Ren'Py could not load the translated game copy: " + str(e))
    rows = []
    for p in sorted(payload.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(payload).as_posix()
        orig = below(root, rel)
        before = digest(orig) if orig.is_file() else None
        after = digest(p)
        if before == after:
            continue
        row = dict(path=rel, before=before, after=after)
        if p.suffix == ".rpyc" and p.with_suffix(".rpy").is_file():
            row["cache_after"] = rpyc_tag(p)
            original_script = orig.with_suffix(".rpy")
            if original_script.is_file():
                import ast, hashlib

                sdk_source = (renpy_sdk().parent / "renpy" / "script.py").read_text(encoding="utf-8")
                magic = re.search(r"(?m)^RPYC_MAGIC\s*=\s*(b[^\n#]+)", sdk_source)
                if magic:
                    row["cache_before"] = hashlib.md5(
                        original_script.read_bytes() + ast.literal_eval(magic[1].strip())
                    ).hexdigest()
                row["source_file"] = original_script.relative_to(root).as_posix()
                row["source_sha"] = digest(original_script)
        rows.append(row)
    unsupported = plan["unsupported"]
    patch = None
    if rows:
        put(staged / "patch.json", dict(game=str(root), files=rows, anchors=plan["anchors"]))
        shutil.copy2(Path(__file__).with_name("patcher.py"), staged / "patcher.py")
        for cmd in ("apply", "restore"):
            (staged / (cmd + ".cmd")).write_bytes(
                (
                    '@echo off\r\npy -3 "%~dp0patcher.py" '
                    + cmd
                    + ' "%~dp0patch.json" %*\r\nexit /b %errorlevel%\r\n'
                ).encode("ascii")
            )
        import hashlib

        key = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:12]
        dest = out / ("patch-" + key)
        if not dest.exists():
            os.replace(staged, dest)
        patch = str(dest / "patch.json")
    if staged.exists() and staged.resolve().parent == work.resolve() and staged.name.startswith("patch-"):
        shutil.rmtree(staged)
    return dict(
        patch=patch,
        engines=plan["engines"],
        native=coverage,
        realtime=True,
        unsupported=unsupported,
        notes=notes,
        live_command="live --game " + str(root),
    )
