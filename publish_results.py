"""Write the bots' latest results into results/ and push them to GitHub.

    python3 publish_results.py              # write results/ and docs/, commit and push if this is a git repo
    python3 publish_results.py --no-push    # just write the files

results/latest.json   everything on the dashboard, as data (leaderboard, holdings, trades, journals, desks)
results/days/DATE.json   the same, kept for each day
results/README.md     a readable summary: leaderboard, today's trades with reasons, journal entries
docs/index.html       the dashboard (turn on GitHub Pages from /docs to open it on your phone)
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from papertrade import Ledger
from papertrade.report import collect

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from dashboard import build  # noqa: E402


def usd(x: float) -> str:
    return f"-${-x:,.0f}" if x < 0 else f"${x:,.0f}"


def summary(report: dict) -> str:
    day = report["as_of"]
    bots = sorted(report["bots"], key=lambda b: -b["equity"])
    lines = [f"# Paper_Trade results, {day}", "", "## Leaderboard", "",
             "| # | Bot | Value | Return | Today | Cash |", "|---|---|---|---|---|---|"]
    for i, b in enumerate(bots, 1):
        lines.append(f"| {i} | {b['name']} | {usd(b['equity'])} | {b['return_pct']:+.2f}% | "
                     f"{usd(b['day_change'])} | {usd(b['cash'])} |")
    lines += ["", f"## Trades on {day}", ""]
    todays = [(b["name"], t) for b in report["bots"] for t in b["trades"] if t["day"] == day]
    if not todays:
        lines.append("No trades today.")
    for name, t in todays:
        value = float(t["quantity"]) * t["price"]
        lines.append(f"- **{name}** {t['side']} {t['quantity']} {t['symbol']} at {t['price']:,.2f} "
                     f"({usd(value)}): {t['reason']}")
    lines += ["", "## Journal entries", ""]
    for b in report["bots"]:
        entry = next((f for f in b["feed"] if f["day"] == day and f["kind"] == "day"), None)
        if entry:
            lines += [f"### {b['name']}", "", entry["text"], ""]
    if report["desks"]:
        lines += ["## Research desks", ""]
        for d in report["desks"]:
            lines.append(f"- **{d['name']}** ({d['day']}): {d['headline']}")
    return "\n".join(lines) + "\n"


def write(ledger_path: str, root: Path = HERE) -> dict:
    report = collect(Ledger(ledger_path))
    if not report["as_of"]:
        raise SystemExit("No trading days in the ledger yet; nothing to publish.")
    (root / "results" / "days").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    data = json.dumps(report, indent=1)
    (root / "results" / "latest.json").write_text(data)
    (root / "results" / "days" / f"{report['as_of']}.json").write_text(data)
    (root / "results" / "README.md").write_text(summary(report))
    build(ledger_path, str(root / "docs" / "index.html"))
    return report


def push(root: Path, day: str) -> str:
    """Commit results/ and docs/ and push. Returns a short status; raises on a failed push."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if git("rev-parse", "--is-inside-work-tree").returncode != 0 or not git("remote").stdout.strip():
        return "not a git repo with a remote; skipped push"
    git("add", "results", "docs")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return "nothing new to push"
    commit = git("commit", "-m", f"Results for {day}")
    if commit.returncode != 0:
        raise SystemExit(f"git commit failed: {commit.stderr or commit.stdout}")
    pushed = git("push")
    if pushed.returncode != 0:
        # Someone else pushed (e.g. a code update): rebase our results commit on top and try once more.
        git("pull", "--rebase")
        pushed = git("push")
    if pushed.returncode != 0:
        raise SystemExit(f"git push failed: {pushed.stderr.strip()}")
    return "pushed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="bots.db")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()
    report = write(args.ledger)
    print(f"wrote results for {report['as_of']}")
    if not args.no_push:
        print(push(HERE, report["as_of"]))


if __name__ == "__main__":
    main()
