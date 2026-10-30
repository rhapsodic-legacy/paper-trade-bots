"""Build a self-contained dashboard page from a ledger and open it in your browser.

    python dashboard.py                        # from bots.db, writes dashboard.html
    python dashboard.py --ledger synthetic.db  # from a synthetic run
    python dashboard.py --no-open              # just write the file

The page has no external dependencies: open it straight from disk, or send it to someone.
Re-run it after each trading day to refresh.
"""
import argparse
import json
import webbrowser
from pathlib import Path

from papertrade import Ledger
from papertrade.report import collect

TEMPLATE = Path(__file__).parent / "papertrade" / "templates" / "dashboard.html"


def build(ledger_path: str, out: str, note: str = "") -> Path:
    report = collect(Ledger(ledger_path))
    report["note"] = note
    data = json.dumps(report)
    # Keep the JSON from closing its own <script> tag, whatever the journals say.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    path = Path(out)
    path.write_text(TEMPLATE.read_text().replace("__DATA__", data))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="bots.db")
    ap.add_argument("--out", default="dashboard.html")
    ap.add_argument("--note", default="", help="a banner line, e.g. to mark demo data")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    path = build(args.ledger, args.out, args.note)
    print(f"wrote {path}")
    if not args.no_open:
        webbrowser.open(path.resolve().as_uri())


if __name__ == "__main__":
    main()
