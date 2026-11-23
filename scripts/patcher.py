"""Portable patch installer. Requires only the Windows Python standard library."""

import argparse
import contextlib
import hashlib
import json
import msvcrt
import os
import shutil
import sys
import tempfile
from pathlib import Path


def sha(path):
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def rpyc_tag(path):
    if not path.is_file() or path.stat().st_size < 26:
        return None
    with path.open("rb") as f:
        if f.read(10) != b"RENPY RPC2":
            return None
        f.seek(-16, 2)
        return f.read(16).hex()


def below(root, name):
    rel = Path(name)
    if rel.is_absolute() or rel.drive or ".." in rel.parts or not rel.parts or ":" in str(rel):
        raise ValueError("Unsafe patch path " + name)
    p = (root / rel).resolve()
    if not p.is_relative_to(root) or p == root:
        raise ValueError("Path escapes game directory")
    return p


def safe_copy(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=dst.parent)
    os.close(handle)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        Path(tmp).unlink(missing_ok=True)


@contextlib.contextmanager
def locked(file):
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def perform(action, manifest, game=None):
    manifest = Path(manifest).resolve()
    spec = json.loads(manifest.read_text(encoding="utf-8-sig"))
    root = Path(game or spec["game"]).resolve(strict=True)
    rows = spec["files"]
    patch_base = manifest.parent
    # Patch identity includes original and new hashes, independent of output location.
    identity = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[
        :20
    ]
    state = below(root, ".hanhua-backup")
    vault = below(root, ".hanhua-backup/" + identity)
    entries = []
    seen = set()
    for r in rows:
        rel = r["path"]
        dst = below(root, rel)
        if dst in seen or dst.is_relative_to(state):
            raise ValueError("Duplicate or reserved target: " + rel)
        seen.add(dst)
        entries.append((r, dst, below(patch_base / "payload", rel), below(vault, rel)))
    with locked(state / "patch.lock"):
        for anchor, expected in spec.get("anchors", {}).items():
            dst = below(root, anchor)
            if dst not in seen and sha(dst) != expected:
                raise ValueError("Game version differs: " + anchor)
        # Preflight all files before changing the first one. A journal permits exact recovery after a crash.
        journal = vault / "journal.json"
        active = journal.is_file()
        if active:
            original_rows = {r["path"]: r for r in json.loads(journal.read_text(encoding="utf-8"))}
            for r, d, p, b in entries:
                r["before"] = original_rows[r["path"]]["before"]
        else:
            for r, d, p, b in entries:
                if (
                    r.get("cache_before")
                    and rpyc_tag(d) == r["cache_before"]
                    and sha(below(root, r["source_file"])) == r["source_sha"]
                ):
                    r["before"] = sha(d)
        if action == "restore" and not active:
            if all(sha(d) == r["before"] for r, d, _, _ in entries):
                return "already restored"
            raise ValueError("Original backup for this patch was not found")
        for r, d, p, b in entries:
            current = sha(d)
            regenerated = active and r.get("cache_after") and rpyc_tag(d) == r["cache_after"]
            if current not in (r["before"], r["after"]) and not regenerated:
                raise ValueError("File was modified: " + r["path"])
            if action == "apply" and sha(p) != r["after"]:
                raise ValueError("Damaged patch file: " + r["path"])
            if action == "apply" and not active and current != r["before"]:
                raise ValueError("Target differs before first application: " + r["path"])
            if active and r["before"] and sha(b) != r["before"]:
                raise ValueError("Missing or damaged backup: " + r["path"])
        if not active:
            for r, d, p, b in entries:
                if r["before"]:
                    safe_copy(d, b)
            vault.mkdir(parents=True, exist_ok=True)
            pending = vault / "journal.tmp"
            pending.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            os.replace(pending, journal)
        for r, d, p, b in entries:
            expect = r["after"] if action == "apply" else r["before"]
            if sha(d) == expect:
                continue
            if action == "apply":
                safe_copy(p, d)
            elif r["before"]:
                safe_copy(b, d)
            else:
                d.unlink(missing_ok=True)
            if sha(d) != expect:
                raise IOError("Write verification failed: " + r["path"])
        if action == "restore":
            journal.unlink(missing_ok=True)
    return "applied" if action == "apply" else "restored"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["apply", "restore"])
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--game", type=Path)
    args = parser.parse_args()
    try:
        print(perform(args.action, args.manifest, args.game))
    except (ValueError, OSError) as e:
        sys.exit(str(e))
