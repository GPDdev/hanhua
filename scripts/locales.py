"""Pair language variants by directory layout, preserving unrelated languages and existing Chinese."""

import copy
import json
import re

import polib

from core import CJK, run
from formats import getat, locres_rows, locres_tool

LANGS = set(
    "en en-us en-gb english ja jp japanese ko korean ru de german fr french it es pt pt-br pl tr nl sv fi cs uk ar th id ms vi zh zh-cn zh-hans zh-sg schinese chs zh-tw zh-hant tchinese".split()
)
SIMPLIFIED = {"zh", "zh-cn", "zh-hans", "zh-sg", "schinese", "chs"}
TRAD = {"zh-tw", "zh-hant", "tchinese"}


def select(root, items):
    groups = {}
    for p in items:
        if p.suffix.lower() not in {".json", ".locres", ".po"}:
            continue
        parts = list(p.relative_to(root).parts)
        pieces = re.split(r"([.])", p.stem)
        lang = None
        for i, x in enumerate(pieces):
            tag = x.lower().replace("_", "-")
            if tag in LANGS:
                pieces[i] = "<locale>"
                lang = tag
                break
        parts[-1] = "".join(pieces) + p.suffix.lower()
        if lang is None:
            for i in range(len(parts) - 2, -1, -1):
                tag = parts[i].lower().replace("_", "-")
                if tag in LANGS:
                    parts[i] = "<locale>"
                    lang = tag
                    break
        if lang:
            groups.setdefault(tuple(parts), []).append((lang, p))
    skip = set()
    pairs = {}
    for entries in groups.values():
        chinese = next((p for lang, p in entries if lang in SIMPLIFIED), None)
        original = next((p for lang, p in entries if lang in {"en", "en-us", "en-gb", "english"}), None)
        original = original or next((p for lang, p in entries if lang not in SIMPLIFIED | TRAD), None)
        skip.update(p for _, p in entries if p != original)
        if original and chinese:
            pairs[original] = chinese
    return skip, pairs


def merged_json(src, old):
    if isinstance(src, dict) and isinstance(old, dict):
        return {**copy.deepcopy(old), **{k: merged_json(val, old.get(k)) for k, val in src.items()}}
    if isinstance(src, list) and isinstance(old, list):
        return [
            merged_json(v, old[i] if i < len(old) else None) for i, v in enumerate(src)
        ] + copy.deepcopy(old[len(src) :])
    return src


def reuse(kind, src, target, work, rows, units):
    # The storage key is independent of the user's filesystem layout.
    import hashlib

    template = work / "locale-templates" / hashlib.sha256(str(src).encode()).hexdigest() / src.name
    template.parent.mkdir(parents=True, exist_ok=True)
    lookup = {u["id"]: u for u in units}
    old = {}
    if kind == "json":
        source = json.loads(src.read_text(encoding="utf-8-sig"))
        localized = json.loads(target.read_text(encoding="utf-8-sig"))
        template.write_text(
            json.dumps(merged_json(source, localized), ensure_ascii=False), encoding="utf-8"
        )
        for r in rows:
            try:
                old[r["id"]] = getat(localized, r["loc"])
            except (KeyError, IndexError, TypeError):
                pass
    elif kind == "po":
        source = polib.pofile(str(src))
        localized = polib.pofile(str(target))
        index = {(e.msgctxt, e.msgid): e for e in localized if not e.obsolete}
        for r in rows:
            i, typ = r["loc"]
            e = index.get((source[i].msgctxt, source[i].msgid))
            if e is not None and "fuzzy" not in e.flags:
                old[r["id"]] = e.msgstr if typ == "single" else e.msgstr_plural.get(0)
        seen = {(e.msgctxt, e.msgid) for e in source}
        for e in localized:
            if (e.msgctxt, e.msgid) not in seen:
                source.append(e)
        source.save(str(template))
    elif kind == "locres":
        original = {x["key"]: x["source"] for x in locres_rows(target)}
        run([locres_tool(), "merge", target, src, "-o", template])
        for r in rows:
            old[r["id"]] = original.get(r["loc"])
    for k, v in old.items():
        if isinstance(v, str) and v.strip() and (v != lookup[k]["source"] or CJK.search(v)):
            lookup[k]["known"] = v
    return template


def godot_files(project, source_root, payload, assets, trans):
    from formats import csv_table

    directory = project.parent
    po = polib.POFile()
    po.metadata = {
        "Language": "zh_CN",
        "Content-Type": "text/plain; charset=UTF-8",
        "Plural-Forms": "nplurals=1; plural=0;",
    }
    messages = {}
    catalogs = []
    for a in assets:
        original = source_root / a["path"]
        if not original.is_relative_to(directory):
            continue
        if a["type"] == "csv":
            table, _ = csv_table(original)
            for r in a["rows"]:
                key = table[r["loc"][0]][0]
                messages[(None, key)] = polib.POEntry(msgid=key, msgstr=trans[r["id"]])
        if a["type"] == "po":
            catalogs.append("res://" + (source_root / a["target"]).relative_to(directory).as_posix())
    if not messages and not catalogs:
        return []
    for e in messages.values():
        po.append(e)
    prefix = directory.relative_to(source_root)
    loc = prefix / "hanhua_locale"
    if any((source_root / loc / name).exists() for name in ("messages.po", "activate.gd")):
        raise ValueError("Godot project already contains a conflicting locale helper file")
    (payload / loc).mkdir(parents=True, exist_ok=True)
    if messages:
        po.save(str(payload / loc / "messages.po"))
        catalogs.append("res://hanhua_locale/messages.po")
    settings = project.read_text(encoding="utf-8-sig")
    newer = bool(re.search(r"(?m)^config_version\s*=\s*5", settings))
    array = "PackedStringArray" if newer else "PoolStringArray"

    def update(text, section, key, transform):
        header = re.compile(r"(?m)^\[" + section + r"\]\s*$")
        h = header.search(text)
        if not h:
            return text.rstrip() + "\n\n[" + section + "]\n" + key + "=" + transform("") + "\n"
        nxt = re.search(r"(?m)^\[", text[h.end() :])
        end = h.end() + nxt.start() if nxt else len(text)
        body = text[h.end() : end]
        pattern = re.compile(r"(?m)^" + re.escape(key) + r"\s*=\s*([^\n]*)")
        old = pattern.search(body)
        line = key + "=" + transform(old.group(1).strip() if old else "")
        body = pattern.sub(lambda _: line, body, count=1) if old else body.rstrip() + "\n" + line + "\n\n"
        return text[: h.end()] + body + text[end:]

    def resources(old):
        additions = [
            json.dumps(c, ensure_ascii=False)
            for c in catalogs
            if json.dumps(c, ensure_ascii=False) not in old
        ]
        if not additions:
            return old
        if not old:
            return array + "(" + ", ".join(additions) + ")"
        if not old.endswith(")"):
            raise ValueError("Unknown Godot translation list representation")
        return old[:-1] + ("" if old[:-1].rstrip().endswith("(") else ", ") + ", ".join(additions) + ")"

    settings = update(settings, "internationalization", "locale/translations", resources)
    settings = update(
        settings, "autoload", "HanhuaLocale", lambda old: '"*res://hanhua_locale/activate.gd"'
    )
    (payload / loc / "activate.gd").write_text(
        'extends Node\n\nfunc _enter_tree():\n    TranslationServer.set_locale("zh_CN")\n', encoding="utf-8"
    )
    (payload / prefix / "project.godot").write_text(settings, encoding="utf-8")
    return [*catalogs, str(loc / "activate.gd"), str(prefix / "project.godot")]
