#!/usr/bin/env python3
"""Generate bundle.json for the sffr-meta repository.

Standard library only, Python 3.9+. Run from the repository root:

    python3 tools/build_bundle.py --out bundle.json

The bundle contains the deck-type vocabulary, every manifest row with its deck
contents, and each period's list statuses, so an archived deck can be judged
against the list of its own period offline.

The bundle is generated automatically by the workflow on every push to main,
after tools/fix_tables.py has repaired the tables. Top-level keys added after
schema 1 was fixed (`categories`) are additive: a reader that does not know
them ignores them, so the schema number is unchanged. `staples` maps every
archived period to the Staples.ydk in force for it (its own folder's copy, else
the nearest earlier one).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate_meta import (  # noqa: E402
    CATEGORIES_FILE, DEFAULT_EVENT_TYPE, MANIFEST_COLUMNS, RECORD_RE, TABLE_SPECS, Report,
    TableSpec, load_deck_types, load_manifest, parse_categories, parse_ydk, scan_periods,
    staples_by_period, table_rows,
)

BUNDLE_SCHEMA = 1

# Optional files
TEXT_DOCUMENTS = [
    "format_notes.md", "eligibility.md", "custom_rulings.md", "edopro_deviations.md",
]
CSV_TABLES = ["deck_roles.csv", "restrictions.csv", "verdicts.csv"]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_commit(repo: Path) -> Optional[str]:
    for env_var in ("GITHUB_SHA",):
        if os.environ.get(env_var):
            return os.environ[env_var]
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return None


def read_csv_table(path: Path) -> List[Dict[str, str]]:
    """Rows keyed by the file's header, every key present: a row with fewer fields than
    the header gets empty strings, so no reader has to tell a missing key from an empty
    value. Blank lines are skipped."""
    text = path.read_bytes().decode("utf-8-sig", "replace")
    spec = TABLE_SPECS.get(path.name)
    if spec is None:
        first = text.splitlines()[0] if text else ""
        spec = TableSpec(path.name, [], ";" if first.count(";") > first.count(",") else ",",
                         False, "X")
    header, rows = table_rows(spec, text)
    return [{k: (v or "").strip() for k, v in values.items()} for _, values, _ in rows]


def read_categories(repo: Path) -> List[Dict[str, str]]:
    path = repo / CATEGORIES_FILE
    if not path.exists():
        return []
    categories, _ = parse_categories(path.read_bytes().decode("utf-8-sig", "replace"))
    return [{"label": c.label, "basis": c.basis, "definition": c.definition} for c in categories]


def build(repo: Path) -> Dict[str, Any]:
    report = Report()
    deck_types = load_deck_types(repo, report, None)
    periods = scan_periods(repo, report)
    rows = load_manifest(repo, report)

    bundle: Dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "commit": git_commit(repo),
        "repository": "XGlitchy30/sffr-meta",
        "manifest_columns": MANIFEST_COLUMNS,
        "deck_types": [],
        "periods": {},
        "entries": [],
        "documents": {},
        "tables": {},
        "categories": read_categories(repo),
        "staples": staples_by_period(periods),
        "counts": {},
    }

    for row in sorted(deck_types.values(), key=lambda r: r.deck_type.lower()):
        bundle["deck_types"].append({
            "deck_type": row.deck_type,
            "aliases": row.aliases,
            "defining_cards": row.defining_cards,
            "description": row.description,
            "kind": row.kind,
        })

    for name, period in sorted(periods.items()):
        list_path = repo / period.list_path
        raw = list_path.read_bytes() if list_path.exists() else b""
        bundle["periods"][name] = {
            "list_file": period.list_path,
            "list_header": period.header,
            "list_sha256": sha256(raw) if raw else None,
            "entries": len(period.statuses),
            "statuses": {str(code): status for code, status in sorted(period.statuses.items())},
        }

    for row in rows:
        rel = row.get("file")
        entry: Dict[str, Any] = {column: row.get(column) or None for column in MANIFEST_COLUMNS}
        entry["period"] = row.get("list_period") or (rel.split("/")[1] if rel.count("/") >= 2 else None)
        entry["engines"] = [e.strip() for e in row.get("engines").split(";") if e.strip()]
        if row.get("kind") == "tournament":
            entry["event_type"] = row.get("event_type") or DEFAULT_EVENT_TYPE
        match = RECORD_RE.match(row.get("record")) if row.get("record") else None
        entry["record_parsed"] = (
            {"wins": int(match.group(1)), "losses": int(match.group(2)), "draws": int(match.group(3))}
            if match else None
        )
        for column in ("placement", "field_size"):
            value = row.get(column)
            entry[column] = int(value) if value.isdigit() else None

        deck_path = repo / rel if rel else None
        if deck_path and deck_path.exists():
            raw = deck_path.read_bytes()
            deck, _ = parse_ydk(raw.decode("utf-8", "replace"))
            entry["deck"] = {"main": deck.main, "extra": deck.extra, "side": deck.side}
            entry["deck_sizes"] = {"main": len(deck.main), "extra": len(deck.extra),
                                   "side": len(deck.side)}
            entry["sha256"] = sha256(raw)
        else:
            entry["deck"] = None
        bundle["entries"].append(entry)

    for name in TEXT_DOCUMENTS:
        path = repo / name
        if path.exists():
            text = path.read_bytes().decode("utf-8-sig", "replace")
            bundle["documents"][name] = {"sha256": sha256(path.read_bytes()), "text": text}

    for name in CSV_TABLES:
        path = repo / name
        if path.exists():
            bundle["tables"][name] = read_csv_table(path)

    kinds: Dict[str, int] = {}
    for entry in bundle["entries"]:
        kinds[entry["kind"] or "?"] = kinds.get(entry["kind"] or "?", 0) + 1
    bundle["counts"] = {
        "deck_types": len(bundle["deck_types"]),
        "periods": len(bundle["periods"]),
        "entries": len(bundle["entries"]),
        "entries_by_kind": dict(sorted(kinds.items())),
        "documents": sorted(bundle["documents"]),
        "tables": sorted(bundle["tables"]),
        "categories": len(bundle["categories"]),
    }
    return bundle


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate bundle.json for sffr-meta.")
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument("--out", default="bundle.json", help="output path (default: bundle.json)")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the committed bundle differs from the generated one")
    parser.add_argument("--indent", type=int, default=None,
                        help="pretty-print with this indent (default: compact)")
    args = parser.parse_args(argv)

    repo = Path(args.repo)
    bundle = build(repo)
    text = json.dumps(bundle, ensure_ascii=False, sort_keys=False,
                      indent=args.indent, separators=(",", ":") if args.indent is None else None)
    text += "\n"

    out = Path(args.out)
    if args.check:
        if not out.exists():
            print(f"{out} does not exist")
            return 1
        current = out.read_text(encoding="utf-8")
        # generated_at differs on every run; compare everything else
        strip = lambda s: re.sub(r'"generated_at":"[^"]*",', "", s)
        if strip(current) != strip(text):
            print(f"{out} is out of date; run tools/build_bundle.py")
            return 1
        print(f"{out} is up to date")
        return 0

    out.write_text(text, encoding="utf-8")
    counts = bundle["counts"]
    print(f"wrote {out} ({out.stat().st_size / 1024:.1f} kB): "
          f"{counts['entries']} entries, {counts['deck_types']} deck types, "
          f"{counts['periods']} periods")
    return 0


if __name__ == "__main__":
    sys.exit(main())