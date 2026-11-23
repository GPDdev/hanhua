"""Shared job storage, translation exchange, asset download and local inference."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path

import requests
from filelock import FileLock

HOME = Path(os.environ.get("HANHUA_HOME") or Path(os.environ["LOCALAPPDATA"]) / "hanhua").resolve()
MODELS = HOME / "models"
TOOLS = HOME / "tools"
VIDEO = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".m4v", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg"}
ZH = {"zh", "zho", "chi", "zh-cn", "zh-hans", "chs", "schinese", "zh_cn", "cmn", "cn"}
CJK = re.compile(r"[\u3400-\u9fff]")
TOKEN = re.compile(
    r"\\[A-Za-z]+\[\d+\]|\\[G{}$.|!><^]|\[\[|\{\{|\[[A-Za-z_][^\]\n]{0,90}\]|\{/?[a-zA-Z0-9_#][^{}\n]{0,85}\}|%(?:\([^)]+\)|\d+\$)?[-+0-9.]*[sdifux]|%\d+|%%|</?[a-zA-Z][^>\n]{0,90}>"
)
SCHEMA_VERSION = 2


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def put(path, value):
    atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def run(args, **kwargs):
    p = subprocess.run([str(x) for x in args], capture_output=True, **kwargs)
    if p.returncode:
        tail = (p.stderr or p.stdout or b"")[-3000:].decode("utf-8", "replace")
        raise RuntimeError(f"{Path(args[0]).name} exited {p.returncode}: {tail}")
    return p.stdout


def ff(*args):
    return run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args])


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path]))


def language(tag):
    return str(tag or "").replace("_", "-").lower()


def chinese_track(tags, audio=False):
    tag = language(tags.get("language"))
    title = tags.get("title", "").lower()
    if any(
        x in title
        for x in ("cantonese", "繁體", "繁体", "粵語", "粤语", "commentary", "评论", "評論", "traditional")
    ) or tag in ("yue", "zh-hk", "zh-tw", "zh-hant"):
        return False
    if audio:
        return tag in {"cmn", "zh-cn", "zh-hans"} or any(
            x in title for x in ("mandarin", "普通话", "国语", "國語")
        )
    return tag in ZH or any(x in title for x in ("简体", "简中", "simplified"))


def download(url, path, sha=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock", timeout=600):
        if path.is_file() and path.stat().st_size and (not sha or digest(path) == sha.lower()):
            return path
        tmp = path.with_suffix(path.suffix + ".partial")
        try:
            for attempt in range(3):
                try:
                    with requests.get(url, stream=True, timeout=(15, 100)) as resp:
                        resp.raise_for_status()
                        with tmp.open("wb") as out:
                            for chunk in resp.iter_content(1024 * 1024):
                                out.write(chunk)
                    if not tmp.stat().st_size or (sha and digest(tmp) != sha.lower()):
                        raise ValueError(f"Download verification failed: {path.name}")
                    os.replace(tmp, path)
                    return path
                except (requests.RequestException, ValueError):
                    if attempt == 2:
                        raise
                    time.sleep(1 + attempt)
        finally:
            tmp.unlink(missing_ok=True)


def tree_extract(archive, dest):
    import tarfile
    import zipfile

    dest = Path(dest).resolve()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as f:
            for n in f.namelist():
                if not (dest / n).resolve().is_relative_to(dest):
                    raise ValueError("Invalid archive path")
            f.extractall(dest)
    else:
        with tarfile.open(archive) as f:
            f.extractall(dest, filter="data")


def unit(source, context, speaker="", kind="text", known=None):
    source = str(source).strip()
    identifier = hashlib.sha256(
        json.dumps([source, context, speaker, kind], ensure_ascii=False).encode()
    ).hexdigest()[:24]
    result = dict(id=identifier, source=source, context=context, speaker=speaker, kind=kind)
    if known:
        result["known"] = known
    return result


def textlike(s):
    return isinstance(s, str) and bool(re.search(r"[^\W\d_]", TOKEN.sub("", s), re.UNICODE))


@lru_cache(maxsize=1)
def chinese_converter():
    from opencc import OpenCC

    return OpenCC("t2s")


def simplified(s):
    if not isinstance(s, str) or not CJK.search(s):
        return s
    parts = []
    start = 0
    for token in TOKEN.finditer(s):
        parts.extend((chinese_converter().convert(s[start : token.start()]), token.group()))
        start = token.end()
    return "".join(parts) + chinese_converter().convert(s[start:])


def validate(u, zh):
    if not isinstance(zh, str) or not zh.strip():
        raise ValueError("Empty translation for " + u["id"])
    if textlike(u["source"]) and not textlike(zh) and not CJK.search(zh):
        raise ValueError("Translation contains no words for " + u["id"])
    if zh.lstrip().startswith(("{", "[")):
        try:
            wrapped = json.loads(zh)
        except ValueError:
            wrapped = None
        if isinstance(wrapped, (dict, list)):
            raise ValueError("Received JSON inside translated text")
    if Counter(TOKEN.findall(u["source"])) != Counter(TOKEN.findall(zh)):
        raise ValueError("Changed formatting tokens for " + u["id"])


def db_open(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "translations.sqlite", timeout=40)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS translations(id TEXT PRIMARY KEY, zh TEXT NOT NULL)")
    return db


def translations(db):
    return dict(db.execute("SELECT id,zh FROM translations"))


def store_batch(db, units, answers):
    indexed = {u["id"]: u for u in units}
    if not isinstance(answers, dict):
        raise ValueError("Expected a JSON object mapping IDs to Chinese text")
    answers = {k: simplified(v) for k, v in answers.items()}
    for k, v in answers.items():
        if k not in indexed:
            raise ValueError("Unknown translation ID: " + str(k))
        validate(indexed[k], v)
    with db:
        db.executemany(
            "INSERT OR REPLACE INTO translations VALUES(?,?)", [(k, v.strip()) for k, v in answers.items()]
        )


def glossary(root):
    p = Path(root) / "glossary.json"
    if not p.is_file():
        put(p, {})
    data = read(p)
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise ValueError("glossary.json must map source terms to Chinese")
    return data


class Ollama:
    def __init__(self, model=None):
        self.url = os.environ.get("HANHUA_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        self.model = model or os.environ.get("HANHUA_MODEL")
        try:
            models = self.tags()
        except requests.ConnectionError:
            if self.url != "http://127.0.0.1:11434" or not shutil.which("ollama"):
                raise
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for _ in range(30):
                time.sleep(0.3)
                try:
                    models = self.tags()
                    break
                except requests.ConnectionError:
                    continue
            else:
                raise RuntimeError("Ollama failed to start")
        if not self.model:
            names = [x["name"] for x in models if "embed" not in x["name"].lower()]
            self.model = next(
                (n for n in names if "qwen" in n.lower()), names[0] if names else "qwen2.5:3b"
            )
        if self.model not in [x["name"] for x in models]:
            resp = requests.post(
                self.url + "/api/pull", json={"name": self.model, "stream": False}, timeout=(10, 1800)
            )
            resp.raise_for_status()
        info = requests.post(self.url + "/api/show", json={"model": self.model}, timeout=15)
        info.raise_for_status()
        meta = info.json()
        architecture = str(meta.get("model_info", {}).get("general.architecture", ""))
        self.raw_chatml = meta.get("template", "").strip() == "{{ .Prompt }}" and (
            architecture.startswith("qwen") or "qwen" in self.model.lower()
        )

    def tags(self):
        r = requests.get(self.url + "/api/tags", timeout=5)
        r.raise_for_status()
        return r.json()["models"]

    def translate(self, units, terms):
        try:
            return self.request(units, terms)
        except (ValueError, KeyError, TypeError):
            if len(units) == 1:
                return self.request(units, terms, single=True)
            mid = len(units) // 2
            return {**self.translate(units[:mid], terms), **self.translate(units[mid:], terms)}

    def request(self, units, terms, single=False):
        masked, maps = [], []
        for u in units:
            tags = []

            def protect(m):
                tags.append(m.group(0))
                return f"⟦{len(tags)}⟧"

            masked.append(TOKEN.sub(protect, u["source"]))
            maps.append(tags)
        rows = [
            {"text": s, "speaker": u["speaker"], "context": u["context"]} for s, u in zip(masked, units)
        ]
        system = (
            "你是专业游戏和影视译者。把每条 text 译成自然简体中文，结合说话人和上下文。严格按原序，"
            "每条只输出译文。保留⟦数字⟧和专有名词，不得附带原文或解释。示例：Save Game → 保存游戏。"
        )
        schema = {
            "type": "object",
            "properties": {
                "zh": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": len(rows),
                    "maxItems": len(rows),
                }
            },
            "required": ["zh"],
        }
        prompt = json.dumps({"术语": terms, "待译": rows}, ensure_ascii=False)
        if single:
            system = "你是专业译者。将原文完整翻译成简体中文。只写译文，不解释。保留⟦数字⟧。"
            if terms:
                system += "\n参考术语：" + json.dumps(terms, ensure_ascii=False)
            prompt = masked[0]
        else:
            system += ' 按 JSON 结构 {"zh":["第一条译文","第二条译文"]} 作答。'
        payload = {"model": self.model, "stream": False, "options": {"temperature": 0, "num_ctx": 8192}}
        if not single:
            payload["format"] = schema
        if self.raw_chatml:
            # Imported Qwen GGUFs may only have a bare completion template in Ollama.
            # Present the same messages using the model's native chat delimiters.
            endpoint = "/api/generate"
            prompt = prompt.replace("<|im_start|>", "〈im_start〉").replace("<|im_end|>", "〈im_end〉")
            payload.update(
                raw=True,
                prompt="<|im_start|>system\n"
                + system
                + "<|im_end|>\n<|im_start|>user\n"
                + prompt
                + "<|im_end|>\n<|im_start|>assistant\n",
            )
            payload["options"]["stop"] = ["<|im_end|>", "<|im_start|>"]
        else:
            endpoint = "/api/chat"
            payload["messages"] = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]
        r = requests.post(self.url + endpoint, json=payload, timeout=(10, 240))
        r.raise_for_status()
        response = r.json()
        answer = response["response"] if self.raw_chatml else response["message"]["content"]
        out = [answer.strip()] if single else json.loads(answer)["zh"]
        if len(out) != len(units):
            raise ValueError("Translation count mismatch")
        result = {}
        for u, zh, tags in zip(units, out, maps):
            for i, tag in enumerate(tags, 1):
                zh = zh.replace(f"⟦{i}⟧", tag)
            zh = simplified(zh)
            validate(u, zh)
            words = re.findall(r"[A-Za-z]+", u["source"])
            if not single and len(words) >= 6 and len(CJK.findall(zh)) <= max(2, len(words) // 3):
                raise ValueError("Possible partial translation for " + u["id"])
            result[u["id"]] = zh
        return result


def translate_pending(db, units, base, engine):
    known = translations(db)
    todo = [u for u in units if u["id"] not in known]
    terms = glossary(base)
    for i in range(0, len(todo), 8):
        group = todo[i : i + 8]
        store_batch(db, group, engine.translate(group, terms))
