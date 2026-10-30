"""Run Paper_Trade's daily routine, and schedule it to run by itself every day.

The routine: pull code updates (if this folder is a git clone), fetch data, run the desks and
bots, build the dashboard, and push the results to GitHub (see publish_results.py).

    python3 daily_run.py                   # run today's routine now: fetch data, run the bots, build the dashboard
    python3 daily_run.py --install         # run it every day at 17:00 (your Mac's local time)
    python3 daily_run.py --install --time 16:30
    python3 daily_run.py --status          # show the schedule and the last few runs
    python3 daily_run.py --uninstall       # stop the daily runs

On a Mac the schedule is a launchd job, so it survives restarts. If the Mac is asleep at the
scheduled time, launchd runs the job as soon as it wakes; if it is shut down, that day is missed.
Every run is logged in logs/, with one line per run in logs/runs.csv, and a failed run shows a
notification. Bots never trade twice on the same day, so a late or repeated run is harmless.
"""
import argparse
import csv
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
LABEL = "com.papertrade.daily"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
STEPS = [
    ("fetch prices and news", ["fetch_data.py"]),
    ("run desks and bots", ["run_bots.py"]),
    ("build dashboard", ["dashboard.py", "--no-open"]),
    ("publish results to GitHub", ["publish_results.py"]),
]
# macOS blocks background jobs from these folders unless Python is given Full Disk Access.
PROTECTED = ["Desktop", "Documents", "Downloads"]


def notify(message: str):
    if platform.system() == "Darwin":
        subprocess.run(["osascript", "-e", f'display notification "{message}" with title "Paper_Trade"'],
                       capture_output=True)


def run_once() -> int:
    LOGS.mkdir(exist_ok=True)
    started = datetime.now()
    log_path = LOGS / f"daily_{started:%Y-%m-%d_%H%M}.log"
    status, failed_step = "ok", ""
    with open(log_path, "w") as log:
        log.write(f"Paper_Trade daily run, started {started:%Y-%m-%d %H:%M:%S}\n")
        if (HERE / ".git").exists():
            # Pick up code updates pushed to GitHub. A failed pull is logged but doesn't stop the run.
            pull = subprocess.run(["git", "pull", "--ff-only"], cwd=HERE, capture_output=True, text=True)
            log.write(f"\n=== get code updates: git pull ===\n{pull.stdout}{pull.stderr}--- exit {pull.returncode} ---\n")
        for name, args in STEPS:
            t0 = time.time()
            log.write(f"\n=== {name}: {' '.join(args)} ===\n")
            log.flush()
            # caffeinate keeps the Mac awake until the step finishes.
            cmd = [sys.executable, str(HERE / args[0]), *args[1:]]
            if shutil.which("caffeinate"):
                cmd = ["caffeinate", "-i", *cmd]
            code = subprocess.run(cmd, cwd=HERE, stdout=log, stderr=subprocess.STDOUT).returncode
            log.write(f"--- {name}: exit {code}, {time.time() - t0:.0f}s ---\n")
            if code != 0:
                status, failed_step = f"failed (exit {code})", name
                break
        log.write(f"\nFinished {datetime.now():%Y-%m-%d %H:%M:%S}: {status}\n")

    runs = LOGS / "runs.csv"
    new = not runs.exists()
    with open(runs, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["started", "finished", "status", "failed_step", "log"])
        w.writerow([f"{started:%Y-%m-%d %H:%M:%S}", f"{datetime.now():%Y-%m-%d %H:%M:%S}", status,
                    failed_step, log_path.name])
    print(f"{status}. Log: {log_path}")
    if status != "ok":
        notify(f"Daily run failed at '{failed_step}'. See logs/{log_path.name}")
    return 0 if status == "ok" else 1


def install(at: str):
    hour, minute = (int(x) for x in at.split(":"))
    if platform.system() != "Darwin":
        print("Automatic install is for macOS. On Linux, add this line with `crontab -e`:\n"
              f"{minute} {hour} * * * cd {HERE} && {sys.executable} {HERE / 'daily_run.py'}")
        return
    blocked = [p for p in PROTECTED if (Path.home() / p) in HERE.parents]
    if blocked:
        sys.exit(f"This folder is inside ~/{blocked[0]}, which macOS won't let a scheduled job read.\n"
                 f"Move it first, for example:  mv {HERE} ~/paper_trade\n"
                 "then run  python3 ~/paper_trade/daily_run.py --install")
    claude = shutil.which("claude")
    if not claude:
        sys.exit("Can't find the `claude` command. Install Claude Code and sign in, then try again.")
    # launchd starts jobs with a bare PATH, so pass on the folders that hold claude and python.
    path = os.pathsep.join(dict.fromkeys([str(Path(claude).parent), str(Path(sys.executable).parent),
                                          "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin",
                                          "/usr/sbin", "/sbin"]))
    LOGS.mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{sys.executable}</string><string>{HERE / 'daily_run.py'}</string></array>
  <key>WorkingDirectory</key><string>{HERE}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{path}</string><key>HOME</key><string>{Path.home()}</string></dict>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
  <key>StandardOutPath</key><string>{LOGS / 'launchd.log'}</string>
  <key>StandardErrorPath</key><string>{LOGS / 'launchd.log'}</string>
</dict>
</plist>
""")
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST)], capture_output=True)  # replace any old schedule
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    print(f"Scheduled: every day at {hour:02d}:{minute:02d} local time ({datetime.now().astimezone():%Z}).\n"
          f"Logs go to {LOGS}. Check with: python3 daily_run.py --status")


def uninstall():
    if PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
        PLIST.unlink()
        print("Daily runs stopped.")
    else:
        print("No daily schedule was installed.")


def status():
    if PLIST.exists():
        text = PLIST.read_text()
        hour = text.split("<key>Hour</key><integer>")[1].split("<")[0]
        minute = text.split("<key>Minute</key><integer>")[1].split("<")[0]
        loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                                capture_output=True).returncode == 0
        print(f"Scheduled every day at {int(hour):02d}:{int(minute):02d} local time"
              f"{'' if loaded else ' (installed but not loaded: run --install again)'}.")
    else:
        print("No daily schedule installed.")
    runs = LOGS / "runs.csv"
    if runs.exists():
        rows = list(csv.DictReader(open(runs)))[-7:]
        print("\nLast runs:")
        for r in rows:
            print(f"  {r['started']}  {r['status']}{'  at ' + r['failed_step'] if r['failed_step'] else ''}")
    else:
        print("No runs yet.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--install", action="store_true", help="run every day at --time")
    ap.add_argument("--time", default="17:00", help="HH:MM, local time (default 17:00, after the US close)")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.install:
        install(args.time)
    elif args.uninstall:
        uninstall()
    elif args.status:
        status()
    else:
        sys.exit(run_once())


if __name__ == "__main__":
    main()
