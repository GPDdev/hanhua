"""Stop a real CLI job after an episode, then resume against the same cache."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import psutil
from core import HOME, digest, put, read, run


def main():
    b = HOME / "verification"
    inputs = b / "resume-input"
    out = Path(tempfile.mkdtemp(prefix="resume-output-", dir=b))
    inputs.mkdir(exist_ok=True)
    for n in (1, 2):
        dest = inputs / f"episode-{n}.mkv"
        if not dest.exists():
            shutil.copy2(b / "cases/Soft.mkv", dest)
    launcher = Path(os.environ["USERPROFILE"]) / ".codex/skills/hanhua/scripts/hanhua.py"
    cmd = [sys.executable, str(launcher), "run", str(inputs), "--out", str(out)]
    first = out / "episode-1.mkv/report.json"
    second = out / "episode-2.mkv/report.json"
    if first.exists():
        raise ValueError(
            "The fixture already has results. Use a fresh HANHUA_HOME for another interruption test."
        )
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 420
    try:
        while not first.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(proc.stderr.read().decode("utf-8", "replace"))
            time.sleep(0.1)
        if not first.exists():
            raise TimeoutError("First episode did not finish")
        if second.is_file():
            raise ValueError("Job finished before the interruption")
        output = Path(read(first)["result"])
        before = (digest(output), output.stat().st_mtime_ns)
    finally:
        if proc.poll() is None:
            parent = psutil.Process(proc.pid)
            descendants = parent.children(recursive=True)
            parent.terminate()
            for p in descendants:
                try:
                    p.terminate()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs([parent, *descendants], timeout=5)
    run(cmd, timeout=420)
    assert second.is_file() and before == (digest(output), output.stat().st_mtime_ns)
    single = cmd[:3] + [str(inputs / "episode-2.mkv"), "--out", str(out / "episode-2.mkv")]
    run(single, timeout=90)
    report = dict(
        interrupted_before_episode_2=True,
        first_output_identical=True,
        single_episode_resume=True,
        second=str(read(second)["result"]),
    )
    put(b / "resume-acceptance.json", report)
    print(report)


if __name__ == "__main__":
    main()
