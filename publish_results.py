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
from datetime import datetime
from pathlib import Path

from papertrade import Ledger
from papertrade.mining import JournalMiner
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
    bench = report.get("benchmark")
    if bench:
        beating = sum(b["equity"] > bench["equity"] for b in bots)
        lines.append(f"| | *{bench['name']}* | {usd(bench['equity'])} | {bench['return_pct']:+.2f}% | "
                     f"{usd(bench['day_change'])} | |")
        lines += ["", f"{beating} of {len(bots)} bots are beating simply buying the S&P 500 on day one and holding it."]
    lines += ["", f"## Trades on {day}", ""]
    todays = [(b["name"], t) for b in report["bots"] for t in b["trades"] if t["day"] == day]
    if not todays:
        lines.append("No trades today.")
    for name, t in todays:
        value = float(t["quantity"]) * t["price"]
        lines.append(f"- **{name}** {t['side']} {t['quantity']} {t['symbol']} at {t['price']:,.2f} "
                     f"({usd(value)}): {t['reason']}")
    triggered = [(name, t) for name, t in todays if t["reason"].startswith("[")]
    if triggered:
        lines += ["", f"{len(triggered)} of these were standing orders (stops, targets or limits) firing."]
    if report.get("standing"):
        lines += ["", "## Standing orders", ""]
        lines += [f"- **{o['bot']}**: {o['order']}" + (f" ({o['reason']})" if o["reason"] else "")
                  for o in report["standing"]]
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
    for mined in JournalMiner(Ledger(ledger_path)).reports():
        (root / "results" / "lessons").mkdir(exist_ok=True)
        start = datetime.strptime(mined["month"], "%B %Y")
        (root / "results" / "lessons" / f"{start:%Y-%m}.md").write_text(lessons_md(mined))
    return report


def lessons_md(m: dict) -> str:
    lines = [f"# What worked in {m['month']}", "", m["summary"], "", "## Which reasoning made money", "",
             "| Reason for opening | Trips | Won | Total profit | Average |", "|---|---|---|---|---|"]
    lines += [f"| {s['category']} | {s['trips']} | {s['wins']} | {usd(s['total_pnl'])} | {s['avg_pct']:+.1f}% |"
              for s in m["stats"]]
    if m["patterns"]:
        lines += ["", "## Patterns", ""] + [f"- {p}" for p in m["patterns"]]
    if m["lessons"]:
        lines += ["", "## Lessons for each bot", ""]
        for bot, lessons in m["lessons"].items():
            lines += [f"**{bot}**", ""] + [f"- {l}" for l in lessons] + [""]
    lines += ["", "## Every round trip", "", "| Bot | Trade | Opened | Closed | Profit | Reason type |",
              "|---|---|---|---|---|---|"]
    lines += [f"| {t['bot']} | {t['direction']} {t['symbol']} | {t['opened']} | {t['closed'] or 'still open'} | "
              f"{usd(t['pnl'])} ({t['pct']:+.1f}%) | {t['category'] or '?'} |" for t in m["trips"]]
    return "\n".join(lines) + "\n"


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
