"""每週自動整理：地點庫自動抓（places.py harvest），再清理對話資料庫（db-maintain.py）。

2026-11-28: 主人問「我只有一個資料庫嗎」，才發現 harvest 和 db-maintain 都要人手動跑，沒有任何排程。

  upkeep.py run              跑一次（Windows 排程每週一 04:07 呼叫這個）
  upkeep.py install-task     註冊 Windows 排程「Hermes Weekly Upkeep」
  upkeep.py status           看上次結果

db-maintain --apply 會停掉 Hermes 閘道（所有 Telegram bot），所以：
- 不能放在 Hermes 自己的 cron（閘道一停，排程自己也停）；用 Windows 排程。
- 最近 ACTIVE_MINUTES 分鐘內有人在跟任何 bot 對話，就跳過這週的清理，不打斷對話。
- 清理時拿著看門狗的鎖（Global\\HermesGatewayWatchdog），看門狗不會在中途把閘道叫起來。
- 結束後閘道沒在跑，就自己再叫起來。
地點庫一定先抓（不停閘道、只會新增或更新，舊的存成 places.backup.json）。結果寫進 upkeep-log.txt 和
upkeep-last.json，週一早上的旅遊週摘要可以讀。
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ACTIVE_MINUTES = 15
TASK_NAME = "Hermes Weekly Upkeep"
WATCHDOG_MUTEX = "Global\\HermesGatewayWatchdog"
MAINTAIN_ARGS = ["--apply", "--restart-gateway", "--archive-pages"]  # never --prune-sessions-days: keeps every conversation


def hermes_root():
    return Path(os.environ.get("HERMES_ROOT") or HERE.parent.parent)


def last_activity(root):
    """Newest message time (epoch seconds) in any profile's state.db, or 0."""
    newest = 0.0
    for path in sorted((Path(root) / "profiles").glob("*/state.db")):
        try:
            db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
            try:
                value = db.execute("SELECT MAX(timestamp) FROM messages").fetchone()[0]
            finally:
                db.close()
        except sqlite3.Error:
            continue
        try:
            newest = max(newest, float(value or 0))
        except (TypeError, ValueError):
            continue
    return newest


class WatchdogLock:
    """Hold the watchdog's named mutex so it does not start the gateway mid-cleanup (Windows only)."""

    def __init__(self, name=WATCHDOG_MUTEX, wait_ms=120000):
        self.name, self.wait_ms, self.handle = name, wait_ms, None

    def __enter__(self):
        if os.name != "nt":
            return self
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.CreateMutexW.restype = ctypes.c_void_p
        self.handle = kernel.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise OSError("拿不到看門狗的鎖")
        result = kernel.WaitForSingleObject(ctypes.c_void_p(self.handle), self.wait_ms)
        if result not in (0, 0x80):  # WAIT_OBJECT_0, WAIT_ABANDONED
            kernel.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None
            raise TimeoutError("看門狗一直在跑，這次不清理")
        return self

    def __exit__(self, *exc):
        if self.handle:
            import ctypes
            kernel = ctypes.windll.kernel32
            kernel.ReleaseMutex(ctypes.c_void_p(self.handle))
            kernel.CloseHandle(ctypes.c_void_p(self.handle))
            self.handle = None
        return False


def run_script(name, *args, timeout=1800):
    """(exit code, last lines of output) for one of the travel-workflow scripts."""
    try:
        done = subprocess.run([sys.executable, str(HERE / name), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return 124, f"超過 {timeout // 60} 分鐘沒跑完"
    text = "\n".join(part.strip() for part in (done.stdout, done.stderr) if part.strip())
    return done.returncode, "\n".join(text.splitlines()[-15:])


def gateway_running(hermes):
    try:
        out = subprocess.run([hermes, "gateway", "status"], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (out.stdout + out.stderr).lower()
    if "no gateway process detected" in text or "not running" in text or "stopped" in text:
        return False
    return "running" in text


def start_gateway(hermes):
    """True when `hermes gateway start` worked."""
    try:
        return subprocess.run([hermes, "gateway", "start"], capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def upkeep(root, *, now=None, run=run_script, activity=last_activity, lock=WatchdogLock,
           hermes=None, gateway=gateway_running, start=start_gateway, clock=time.time):
    """One weekly pass. Returns the result dict (also what upkeep-last.json holds)."""
    result = {"at": dt.datetime.fromtimestamp(now or clock()).isoformat(timespec="seconds")}
    code, text = run("places.py", "harvest")
    result["harvest"] = {"ok": code == 0, "summary": text}
    # The time after the harvest: someone may have written meanwhile (Antigravity's review: idle came out negative).
    idle = max(0.0, ((now or clock()) - activity(root)) / 60)
    if idle < ACTIVE_MINUTES:
        result["maintain"] = {"ok": False, "skipped": f"{round(idle)} 分鐘前還有人在對話，這週不清理"}
        return result
    hermes = hermes or shutil.which("hermes")
    try:
        with lock():
            code, text = run("db-maintain.py", *MAINTAIN_ARGS)
            result["maintain"] = {"ok": code == 0, "summary": text}
            # db-maintain restarts it; make sure while the watchdog is still held off (Antigravity's review)
            if hermes and gateway(hermes) is False:
                result["gateway_started_again"] = start(hermes)
    except (OSError, TimeoutError) as error:
        result.setdefault("maintain", {"ok": False, "skipped": str(error)})
    return result


def record(root, result):
    folder = Path(root) / "shared" / "travel-workflow"
    (folder / "upkeep-last.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"===== {result['at']}"]
    for part in ("harvest", "maintain"):
        info = result.get(part, {})
        lines.append(f"[{part}] " + ("OK" if info.get("ok") else "略過" if info.get("skipped") else "失敗"))
        lines.append(info.get("skipped") or info.get("summary") or "")
    with open(folder / "upkeep-log.txt", "a", encoding="utf-8") as log:
        log.write("\n".join(lines) + "\n")


def install_task(day="MON", at="04:07"):
    """Register (or replace) the weekly Windows task; pythonw so no window pops up."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.is_file() else Path(sys.executable)
    command = f'"{exe}" "{Path(__file__).resolve()}" run'
    done = subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/SC", "WEEKLY", "/D", day,
                           "/ST", at, "/TR", command], capture_output=True, text=True, errors="replace")
    print((done.stdout + done.stderr).strip())
    return done.returncode


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:  # pythonw (the weekly task) has neither (Antigravity's review)
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run")
    task = sub.add_parser("install-task")
    task.add_argument("--day", default="MON")
    task.add_argument("--at", default="04:07")
    sub.add_parser("status")
    args = parser.parse_args()
    root = hermes_root()
    if args.command == "install-task":
        return install_task(args.day, args.at)
    if args.command == "status":
        path = root / "shared" / "travel-workflow" / "upkeep-last.json"
        print(path.read_text(encoding="utf-8") if path.is_file() else "還沒跑過")
        return 0
    result = upkeep(root)
    record(root, result)
    if sys.stdout is not None:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result["harvest"]["ok"] and (result["maintain"].get("ok") or result["maintain"].get("skipped")) else 1


if __name__ == "__main__":
    sys.exit(main())
