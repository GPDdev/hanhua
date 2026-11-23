import ast
import csv
import io
import json
import re
import tempfile
from pathlib import Path

import polib

from core import TOOLS, download, run, textlike, unit


def entry(rows, units, loc, text, ctx, role="", known=None):
    if not textlike(text):
        return
    u = unit(text, ctx, role, known=known)
    units.append(u)
    rows.append({"loc": loc, "id": u["id"]})


def json_strings(data, path=()):
    if isinstance(data, str):
        yield list(path), data
    elif isinstance(data, dict):
        for k, v in data.items():
            if k.lower() not in {
                "id",
                "key",
                "guid",
                "path",
                "file",
                "font",
                "icon",
                "type",
                "locale",
                "language",
                "version",
            }:
                yield from json_strings(v, path + (k,))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            yield from json_strings(v, path + (i,))


RPG_FIELDS = {
    "name",
    "nickname",
    "description",
    "profile",
    "message1",
    "message2",
    "message3",
    "message4",
    "currencyUnit",
    "gameTitle",
}
SYSTEM_ARRAYS = {"elements", "equipTypes", "skillTypes", "weaponTypes", "armorTypes", "terms"}


def rpg_strings(obj, table, path=()):
    if isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from rpg_strings(v, table, path + (i,))
    elif isinstance(obj, dict):
        if isinstance(obj.get("code"), int) and "parameters" in obj:
            args = obj["parameters"]
            code = obj["code"]
            slots = {401: [0], 405: [0], 402: [1], 101: [4], 320: [1], 324: [1], 325: [1]}
            if code == 102 and args and isinstance(args[0], list):
                for i, t in enumerate(args[0]):
                    if isinstance(t, str):
                        yield list(path + ("parameters", 0, i)), t
            for i in slots.get(code, []):
                if i < len(args) and isinstance(args[i], str):
                    yield list(path + ("parameters", i)), args[i]
            return
        for k, v in obj.items():
            record = (
                table in {"Actors", "Armors", "Classes", "Enemies", "Items", "Skills", "States", "Weapons"}
                and len(path) == 1
            )
            system = table == "System" and not path
            map_name = not path and table.startswith("Map") and k == "displayName"
            if isinstance(v, str) and (
                (record and k in RPG_FIELDS) or (system and k in {"gameTitle", "currencyUnit"}) or map_name
            ):
                yield list(path + (k,)), v
            elif system and k in SYSTEM_ARRAYS:
                yield from json_strings(v, path + (k,))
            elif isinstance(v, (dict, list)):
                yield from rpg_strings(v, table, path + (k,))


def getat(obj, loc):
    for k in loc:
        obj = obj[k]
    return obj


def putat(obj, loc, val):
    parent = getat(obj, loc[:-1])
    parent[loc[-1]] = val


def locres_tool():
    return download(
        "https://github.com/akintos/UnrealLocres/releases/download/1.1.1/UnrealLocres.exe",
        TOOLS / "UnrealLocres-1.1.1.exe",
        "b961927f92a8bca928d378a8430e5ea36fed300500b228b184b8d6b61ebb22a1",
    )


def locres_rows(path):
    with tempfile.TemporaryDirectory() as tmp:
        csvpath = Path(tmp) / "strings.csv"
        run([locres_tool(), "export", path, "-o", csvpath])
        with csvpath.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))


QUOTED = re.compile(r'^(?P<pre>.*?)(?P<q>"(?:[^"\\]|\\.)*")(?P<tail>\s*(?:with\s+[\w.]+)?\s*)$')


def quoted(line):
    m = QUOTED.match(line.rstrip("\r\n"))
    if not m:
        return None
    try:
        s = ast.literal_eval(m["q"])
    except (ValueError, SyntaxError):
        return None
    return m, s


def csv_table(path):
    text = path.read_text(encoding="utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text, newline=""), dialect)), dialect


def collect(kind, path, ctx, units):
    rows = []
    if kind in ("rpg", "json"):
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        it = rpg_strings(data, path.stem) if kind == "rpg" else json_strings(data)
        for loc, text in it:
            if loc and textlike(text):
                entry(rows, units, loc, text, ctx + " / " + str(loc[-1]) if kind == "json" else ctx)
    elif kind == "csv":
        data, _ = csv_table(path)
        if not data:
            return []
        headers = [x.strip().lower().replace("_", "-") for x in data[0]]
        if len(headers) < 2 or headers[0] not in {"key", "keys", "id", "stringid"}:
            return []
        zh = next((i for i, h in enumerate(headers) if h in {"zh-cn", "zh-hans", "zh", "schinese"}), None)
        source = next(
            (i for i, h in enumerate(headers) if h in ("en", "en-us", "en-gb", "source", "english")),
            next((i for i in range(1, len(headers)) if i != zh), None),
        )
        if source is None:
            return []
        for i, row in enumerate(data[1:], 1):
            if source < len(row):
                entry(
                    rows,
                    units,
                    [i, zh if zh is not None else len(headers)],
                    row[source],
                    ctx + " / " + row[0],
                    known=(row[zh] if zh is not None and zh < len(row) else None),
                )
    elif kind == "po":
        po = polib.pofile(str(path))
        for i, item in enumerate(po):
            if item.obsolete or not item.msgid:
                continue
            if item.msgid_plural:
                text = item.msgstr_plural.get(1) or item.msgstr_plural.get(0) or item.msgid_plural
                entry(rows, units, [i, "plural"], text, ctx + " / " + (item.msgctxt or item.msgid))
            else:
                entry(
                    rows,
                    units,
                    [i, "single"],
                    item.msgstr or item.msgid,
                    ctx + " / " + (item.msgctxt or item.msgid),
                )
    elif kind == "renpy":
        last = None
        for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines()):
            stripped = line.strip()
            if stripped.startswith("translate "):
                last = None
            if stripped.startswith(("# ", "old ")):
                item = quoted(stripped[2:] if stripped.startswith("# ") else stripped[4:])
                if item:
                    last = (item[1], item[0]["pre"].strip())
                continue
            if last and stripped and not stripped.startswith("#"):
                item = quoted(line)
                if item:
                    if not item[1] or item[1] == last[0]:
                        entry(rows, units, i, last[0], ctx, last[1])
                    last = None
    elif kind == "locres":
        for item in locres_rows(path):
            entry(rows, units, item["key"], item["source"], ctx + " / " + item["key"])
    elif kind == "unity":
        import UnityPy

        env = UnityPy.load(str(path))
        for obj in env.objects:
            if obj.type.name != "TextAsset":
                continue
            txt = obj.parse_as_object()
            raw = txt.m_Script
            if (
                not isinstance(raw, str)
                or not raw.strip()
                or "\0" in raw
                or any(0xD800 <= ord(c) <= 0xDFFF for c in raw)
            ):
                continue
            identity = " ".join(
                (txt.m_Name, str(getattr(txt, "m_PathName", "") or ""), str(obj.container or ""))
            )
            if not (
                re.search(
                    r"dialog|text|lang|local|story|strings?|captions?|subtitles?|menu|tooltip|quest",
                    identity,
                    re.I,
                )
                or re.fullmatch(r"(en|ja|fr|de|ko|ru)([_-]\w+)?", txt.m_Name, re.I)
            ):
                continue
            asset = [str(obj.assets_file.name), obj.path_id]
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            if isinstance(data, (dict, list)):
                for loc, t in json_strings(data):
                    entry(rows, units, asset + [loc], t, ctx + " / " + txt.m_Name)
            elif re.search(r"dialog|text|language|local|story|string", txt.m_Name, re.I) and not re.search(
                r"using UnityEngine|<\\?xml|function\s*\(", raw
            ):
                entry(rows, units, asset + [None], raw, ctx + " / " + txt.m_Name)
    return rows


def rewrite(kind, src, dst, rows, trans):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if kind in ("rpg", "json"):
        data = json.loads(src.read_text(encoding="utf-8-sig"))
        for r in rows:
            putat(data, r["loc"], trans[r["id"]])
        dst.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        check = json.loads(dst.read_text(encoding="utf-8"))
        assert all(getat(check, r["loc"]) == trans[r["id"]] for r in rows)
    elif kind == "csv":
        data, dialect = csv_table(src)
        if rows and rows[0]["loc"][1] == len(data[0]):
            data[0].append("zh_CN")
        for r in rows:
            i, j = r["loc"]
            while len(data[i]) <= j:
                data[i].append("")
            data[i][j] = trans[r["id"]]
        with dst.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f, dialect).writerows(data)
        reopened, _ = csv_table(dst)
        assert all(getat(reopened, r["loc"]) == trans[r["id"]] for r in rows)
    elif kind == "po":
        po = polib.pofile(str(src))
        po.metadata.update(
            {
                "Language": "zh_CN",
                "Plural-Forms": "nplurals=1; plural=0;",
                "Content-Type": "text/plain; charset=UTF-8",
            }
        )
        for r in rows:
            i, mode = r["loc"]
            item = po[i]
            if mode == "plural":
                item.msgstr_plural = {0: trans[r["id"]]}
            else:
                item.msgstr = trans[r["id"]]
            if "fuzzy" in item.flags:
                item.flags.remove("fuzzy")
        po.save(str(dst))
        check = polib.pofile(str(dst))
        assert all(
            (check[r["loc"][0]].msgstr if r["loc"][1] == "single" else check[r["loc"][0]].msgstr_plural[0])
            == trans[r["id"]]
            for r in rows
        )
    elif kind == "renpy":
        lines = src.read_text(encoding="utf-8-sig").splitlines()
        for r in rows:
            i = r["loc"]
            m, _ = quoted(lines[i])
            lines[i] = m["pre"] + json.dumps(trans[r["id"]], ensure_ascii=False) + m["tail"]
        dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert all(quoted(lines[r["loc"]])[1] == trans[r["id"]] for r in rows)
    elif kind == "locres":
        items = locres_rows(src)
        values = {r["loc"]: trans[r["id"]] for r in rows}
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "import.csv"
            with p.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["key", "source", "target"])
                writer.writeheader()
                for it in items:
                    it["target"] = values.get(it["key"], it["source"])
                    writer.writerow(it)
            run([locres_tool(), "import", src, p, "-o", dst])
        check = {it["key"]: it["source"] for it in locres_rows(dst)}
        assert all(check[k] == v for k, v in values.items())
    elif kind == "unity":
        import UnityPy

        env = UnityPy.load(str(src))
        edits = {}
        for r in rows:
            edits.setdefault(tuple(r["loc"][:2]), []).append(r)
        expected = {}
        for obj in env.objects:
            key = (str(obj.assets_file.name), obj.path_id)
            if key not in edits:
                continue
            txt = obj.parse_as_object()
            group = edits[key]
            if group[0]["loc"][2] is None:
                txt.m_Script = trans[group[0]["id"]]
            else:
                data = json.loads(txt.m_Script)
                for r in group:
                    putat(data, r["loc"][2], trans[r["id"]])
                txt.m_Script = json.dumps(data, ensure_ascii=False)
            txt.save()
            expected[key] = txt.m_Script
        dst.write_bytes(env.file.save())
        # Use an explicitly named in-memory reader so no Windows handle survives
        # verification when the staged patch directory is renamed.
        reopened = UnityPy.Environment()
        reopened.load_file(dst.read_bytes(), name=dst.name)
        # SerializedFile names can follow the outer filename; compare object ids within each file.
        values = {
            (str(o.assets_file.name), o.path_id): o.parse_as_object().m_Script
            for o in reopened.objects
            if o.type.name == "TextAsset"
        }
        assert all(
            values.get(k) == v
            or (
                len(env.files) == 1
                and (str(dst.name), k[1]) in values
                and values[(str(dst.name), k[1])] == v
            )
            for k, v in expected.items()
        )
