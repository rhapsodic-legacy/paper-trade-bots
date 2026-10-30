"""Read the bots' journals: daily entries plus weekly and monthly summaries, newest first.

    python journal_feed.py                      # every bot, printed
    python journal_feed.py "Bitcoin Bob"        # one bot
    python journal_feed.py --markdown journals  # write journals/<bot>.md files
    python journal_feed.py --ledger synthetic.db
"""
import argparse
from pathlib import Path

from papertrade import Ledger
from papertrade.journal import JournalMemory

ICONS = {"day": "", "week": "📅 ", "month": "🗓️ "}


def render(name: str, feed) -> str:
    out = [f"# {name}'s journal", ""]
    for _, kind, title, text in feed:
        out += [f"## {ICONS[kind]}{title}", "", text, ""]
    if not feed:
        out.append("No entries yet.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bots", nargs="*")
    ap.add_argument("--ledger", default="bots.db")
    ap.add_argument("--markdown", metavar="DIR", help="write one Markdown file per bot into DIR")
    args = ap.parse_args()

    ledger = Ledger(args.ledger)
    memory = JournalMemory(ledger)
    for name in args.bots or ledger.account_names():
        text = render(name, memory.feed(name))
        if args.markdown:
            path = Path(args.markdown) / f"{name.lower().replace(' ', '_').replace('-', '_')}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            print(f"wrote {path}")
        else:
            print(text)


if __name__ == "__main__":
    main()
