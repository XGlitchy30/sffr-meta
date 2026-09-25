#!/usr/bin/env python3
"""Repair mechanical defects in the sffr-meta tables, in place.

Standard library only, Python 3.9+. Run from the repository root:

    python3 tools/fix_tables.py              # repair and report
    python3 tools/fix_tables.py --dry-run    # report only, write nothing

The workflow runs it before tools/validate_meta.py and commits whatever it
changed, so a table with a missing trailing comma or a stray blank line never
has to be re-uploaded by hand. It only makes changes whose result is certain:

  every table (manifest.csv, deck_types.csv, restrictions.csv, verdicts.csv)
    * UTF-8 BOM added or removed as the file requires; CRLF and bare CR -> LF;
    * blank lines removed; trailing whitespace after the last field removed;
    * a row with fewer fields than the header padded with empty trailing fields;
    * a row whose surplus fields are all empty trimmed back to the header;
    * rows sorted into the documented order (not manifest.csv, which has none);
      skipped while any row still has the wrong number of fields;
    * exactly one trailing newline.
  restrictions.csv only
    * a passcode that is provably wrong is replaced. Provably wrong means: two
      rows share (card_id, list_period); the row's card_name is not the name the
      period's list gives that passcode; exactly one other passcode on that list
      carries the row's card_name; and no row already uses that passcode for
      that period. Anything less certain is left to validate_meta.py, which
      reports it with the suggested passcode (R003, R014).
  decks/<period>/Staples.ydk
    * a copy identical to the collection already in force (the nearest earlier
      Staples.ydk; Main and Side compared as one section, since their split
      carries no meaning) is deleted together with its manifest.csv row. A
      period folder holds Staples.ydk only when the staples changed; later
      periods inherit it.
  Markdown documents (categories.md and the optional documents)
    * BOM removed, CRLF and bare CR -> LF, exactly one trailing newline.

Record text is edited in place, never re-serialised, so quoting, spacing and
field contents of untouched rows stay byte-identical.

Exit codes: 0 always (nothing to fix is not an error), 2 usage error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate_meta import (  # noqa: E402
    CATEGORIES_FILE, TABLE_SPECS, Period, Record, Report, TableSpec, norm_name,
    normalise_newlines, redundant_staples, scan_periods, split_records,
)

MARKDOWN_FILES = [CATEGORIES_FILE, "format_notes.md", "eligibility.md", "custom_rulings.md",
                  "edopro_deviations.md"]
BOM = "\ufeff"


class Change:
    def __init__(self, path: str, line: Optional[int], message: str):
        self.path, self.line, self.message = path, line, message

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"FIXED   {where}  {self.message}"


# --------------------------------------------------------------------------- #
# text mechanics
# --------------------------------------------------------------------------- #

def fix_text(rel: str, text: str, require_bom: bool, changes: List[Change]) -> Tuple[str, bool]:
    """Return (body without BOM, LF only, no trailing newline, whether a BOM is wanted)."""
    had_bom = text.startswith(BOM)
    body = text[1:] if had_bom else text
    if require_bom and not had_bom:
        changes.append(Change(rel, None, "added the UTF-8 BOM this file requires"))
    if had_bom and not require_bom:
        changes.append(Change(rel, None, "removed a UTF-8 BOM"))
    if "\r" in body:
        kind = "CRLF" if "\r\n" in body else "bare CR"
        changes.append(Change(rel, None, f"converted {kind} line endings to LF"))
        body = normalise_newlines(body)
    stripped = body.rstrip("\n")
    if body != stripped + "\n" and stripped:
        changes.append(Change(rel, None, "set exactly one trailing newline"))
    return stripped, require_bom


def fix_markdown(repo: Path, rel: str, changes: List[Change]) -> Optional[str]:
    path = repo / rel
    if not path.exists():
        return None
    text = path.read_bytes().decode("utf-8", "replace")
    local: List[Change] = []
    body, _ = fix_text(rel, text, False, local)
    if not local:
        return None
    changes += local
    return body + "\n"


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #

def _pad(rec: Record, spec: TableSpec, rel: str, changes: List[Change]) -> Tuple[Record, bool]:
    """Pad or trim a record to the header width. Returns (record, width now correct)."""
    width = len(spec.columns)
    n = len(rec.values)
    if n == width:
        return rec, True
    if n < width:
        missing = width - n
        changes.append(Change(rel, rec.line,
                              f"row had {n} fields; added {missing} empty trailing field(s)"))
        return Record(rec.line, rec.raw + spec.delimiter * missing,
                      rec.values + [""] * missing), True
    surplus = rec.values[width:]
    if all(not v.strip() for v in surplus) and rec.raw.endswith(spec.delimiter * len(surplus)):
        changes.append(Change(rel, rec.line,
                              f"row had {n} fields; removed {len(surplus)} empty surplus field(s)"))
        return Record(rec.line, rec.raw[: -len(surplus)], rec.values[:width]), True
    return rec, False


def _repair_restriction_ids(records: List[Record], spec: TableSpec, periods: Dict[str, Period],
                            rel: str, changes: List[Change]) -> List[Record]:
    cols = spec.columns
    i_id, i_name, i_period = cols.index("card_id"), cols.index("card_name"), cols.index("list_period")
    keys: Dict[Tuple[str, str], List[int]] = {}
    for idx, rec in enumerate(records):
        if len(rec.values) == len(cols):
            keys.setdefault((rec.values[i_id].strip(), rec.values[i_period].strip()), []).append(idx)
    taken = set(keys)
    out = list(records)
    for (card_id, period_name), members in sorted(keys.items()):
        period = periods.get(period_name)
        if len(members) < 2 or period is None or not card_id.isdigit():
            continue
        code = int(card_id)
        for idx in members:
            rec = out[idx]
            name = rec.values[i_name].strip()
            own = period.comments.get(code, "")
            if not name or norm_name(own) == norm_name(name):
                continue
            hits = [c for c, comment in period.comments.items()
                    if c != code and norm_name(comment) == norm_name(name)]
            if len(hits) != 1 or (str(hits[0]), period_name) in taken:
                continue
            first = rec.raw.split(spec.delimiter, 1)
            if len(first) != 2 or first[0].strip() != card_id:
                continue                         # quoted or padded id: leave it to a human
            new_id = str(hits[0])
            values = list(rec.values)
            values[i_id] = new_id
            out[idx] = Record(rec.line, new_id + spec.delimiter + first[1], values)
            taken.add((new_id, period_name))
            changes.append(Change(rel, rec.line,
                                  f"card_id {card_id} -> {new_id}: ({card_id}, {period_name}) was "
                                  f"duplicated and the {period_name} list names {card_id} "
                                  f"'{own}' but {new_id} '{period.comments[hits[0]]}'"))
    return out


def fix_table(repo: Path, spec: TableSpec, periods: Dict[str, Period],
              changes: List[Change], drop_files: Optional[Dict[str, str]] = None) -> Optional[str]:
    rel = spec.name
    path = repo / rel
    if not path.exists():
        return None
    original = path.read_bytes().decode("utf-8", "replace")
    local: List[Change] = []
    body, bom = fix_text(rel, original, spec.require_bom, local)
    records = split_records(body, spec.delimiter)
    if not records:
        return None
    header, data = records[0], records[1:]

    header_raw = header.raw.rstrip()
    if header_raw != header.raw:
        local.append(Change(rel, 1, "removed trailing whitespace from the header"))

    kept: List[Record] = []
    widths_ok = True
    for rec in data:
        if not rec.values or all(not v.strip() for v in rec.values):
            local.append(Change(rel, rec.line, "removed a blank line"))
            continue
        raw = rec.raw.rstrip(" \t")
        if raw != rec.raw:
            local.append(Change(rel, rec.line, "removed trailing whitespace"))
            rec = Record(rec.line, raw, rec.values)
        rec, ok = _pad(rec, spec, rel, local)
        widths_ok = widths_ok and ok
        if drop_files and rel == "manifest.csv" and rec.values and rec.values[0].strip() in drop_files:
            local.append(Change(rel, rec.line, f"removed the row of {rec.values[0].strip()}, "
                                               f"identical to {drop_files[rec.values[0].strip()]}"))
            continue
        kept.append(rec)

    if rel == "restrictions.csv":
        kept = _repair_restriction_ids(kept, spec, periods, rel, local)

    if spec.sort_key and widths_ok:
        keyed = [(spec.sort_key(dict(zip(spec.columns, [v.strip() for v in r.values]))), n, r)
                 for n, r in enumerate(kept)]
        ordered = [r for _, _, r in sorted(keyed, key=lambda t: (t[0], t[1]))]
        if [r.line for r in ordered] != [r.line for r in kept]:
            moved = sum(1 for a, b in zip(ordered, kept) if a.line != b.line)
            local.append(Change(rel, None, f"sorted the rows into the documented order "
                                           f"({moved} row(s) moved)"))
            kept = ordered
    elif spec.sort_key and not widths_ok:
        local.append(Change(rel, None, "sorting skipped: a row still has the wrong number of "
                                       "fields (see validate_meta.py)"))

    text = (BOM if bom else "") + "\n".join([header_raw] + [r.raw for r in kept]) + "\n"
    if text == original:
        return None
    if not local:
        local.append(Change(rel, None, "normalised the file"))
    changes += local
    return text


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def run(repo: Path, dry_run: bool) -> List[Change]:
    periods = scan_periods(repo, Report())
    changes: List[Change] = []
    rewrites: Dict[str, str] = {}
    redundant = redundant_staples(repo, periods)
    for rel, previous in sorted(redundant.items()):
        changes.append(Change(rel, None, f"deleted: identical to {previous}, which later periods "
                                         "inherit"))
    for spec in TABLE_SPECS.values():
        text = fix_table(repo, spec, periods, changes, redundant)
        if text is not None:
            rewrites[spec.name] = text
    for rel in MARKDOWN_FILES:
        text = fix_markdown(repo, rel, changes)
        if text is not None:
            rewrites[rel] = text
    if not dry_run:
        for rel, text in rewrites.items():
            (repo / rel).write_bytes(text.encode("utf-8"))
        for rel in redundant:
            (repo / rel).unlink()
    return changes


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Repair mechanical defects in the sffr-meta tables.")
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument("--dry-run", action="store_true", help="report the repairs, write nothing")
    parser.add_argument("--github", action="store_true",
                        help="emit GitHub Actions annotations (default when GITHUB_ACTIONS is set)")
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        args.github = True
    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"not a directory: {repo}", file=sys.stderr)
        return 2

    changes = run(repo, args.dry_run)
    for change in changes:
        print(change)
        if args.github:
            line = f",line={change.line}" if change.line else ""
            print(f"::notice file={change.path}{line},title=Auto-fixed::{change.message}")
    verb = "would change" if args.dry_run else "changed"
    files = sorted({c.path for c in changes})
    summary = (f"fix_tables: {len(changes)} repair(s); {verb} {', '.join(files)}"
               if changes else "fix_tables: nothing to repair")
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary and changes:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write("## Automatic repairs\n\n| File | Line | Repair |\n|---|---|---|\n")
            for c in changes:
                fh.write(f"| `{c.path}` | {c.line or ''} | {c.message} |\n")
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
