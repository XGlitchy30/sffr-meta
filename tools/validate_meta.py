#!/usr/bin/env python3

"""Validate the sffr-meta repository.

Standard library only, Python 3.9+. Run from the repository root:

    python3 tools/validate_meta.py
    python3 tools/validate_meta.py --live-lflist /tmp/sffr.lflist.conf
    python3 tools/validate_meta.py --json report.json --strict

Exit codes: 0 no errors, 1 errors found (or warnings with --strict), 2 usage error.

Mechanical defects of the CSV files (blank lines, rows missing trailing empty
fields, BOM, CRLF, sort order) are repaired by tools/fix_tables.py, which the
workflow runs before this validator. What is still reported here is what needs
a human decision.

Check codes
    F###  file mechanics (encoding, line endings, trailing newline)
    M###  manifest.csv rows
    E###  per-event consistency (placements, records, field sizes)
    D###  .ydk deck files and their legality under their own period list
    T###  deck_types.csv
    P###  period folders and their list files
    C###  categories.md (the restriction-category taxonomy)
    R###  restrictions.csv
    V###  verdicts.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

VALIDATOR_VERSION = "1.1.0"

MANIFEST_COLUMNS = [
    "file", "kind", "date", "event", "event_type", "placement", "field_size",
    "record", "pilot", "deck_type", "engines", "list_period", "notes",
]
DECK_TYPES_COLUMNS = ["deck_type", "aliases", "defining_cards", "description", "kind"]
RESTRICTION_COLUMNS = [
    "card_id", "card_name", "list_period", "status", "category", "rationale", "evidence",
    "source", "notes",
]
VERDICT_COLUMNS = [
    "verdict_id", "date", "list_period", "subject", "card_ids", "subject_kind", "question",
    "verdict", "confidence", "authority", "category", "grounds", "anchors", "supersedes",
    "variant_of", "notes",
]
CATEGORIES_FILE = "categories.md"
STAPLES_FILE = "Staples.ydk"   # present only in the folders of periods where the staples changed

RESTRICTION_STATUSES = {"0", "1", "2", "3", "absent"}
VERDICT_VALUES = {"0", "1", "2", "3"}      # the copy limit decided; 0 = not eligible
EVIDENCE_VALUES = {"stated", "inferred", "unknown"}
CONFIDENCE_VALUES = {"high", "medium", "low"}
AUTHORITY_VALUES = {"maintainer", "antonio", "claude"}
SUBJECT_KINDS = {"official", "custom", "proposal", "package"}

KINDS = {"tournament", "community", "brew", "reference"}
EVENT_TYPES = {"Double-Elim", "Swiss", "Round Robin"}
DEFAULT_EVENT_TYPE = "Double-Elim"
DECK_TYPE_KINDS = {"deck", "engine", "both"}

PERIOD_RE               = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
DATE_RE                 = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RECORD_RE               = re.compile(r"^(\d+)-(\d+)-(\d+)$")
LIST_HEADER_RE          = re.compile(r"^!(\d{4})\.(\d{2})\b")
BARE_PASSCODE_RE        = re.compile(r"(?<!\w)\d{5,}(?!\w)")
FORBIDDEN_NAME_CHARS    = set(";|&()#*!:")
VERDICT_ID_RE           = re.compile(r"^V\d{3,}$")
ID_LIST_RE              = re.compile(r"^\d+(;\d+)*$")
CATEGORY_LABEL_RE       = re.compile(r"^[a-z][a-z0-9-]*$")

NUMERIC_FIELDS = {"level", "rank", "link", "scale", "atk", "def"}
ENUM_FIELDS = {
    "card_type": {"monster", "spell", "trap"},
    "subtype": {
        "effect", "normal", "tuner", "continuous", "fusion", "synchro", "xyz",
        "equip", "flip", "field", "quick-play", "ritual", "special summon",
        "pendulum", "gemini", "link", "spirit", "union", "counter", "toon",
    },
}
OPEN_FIELDS = {"race", "attribute"}
ALL_FIELDS = NUMERIC_FIELDS | set(ENUM_FIELDS) | OPEN_FIELDS
NUMERIC_VALUE_RE = re.compile(r"^(>=|<=|>|<|=)?-?\d+$|^-?\d+\.\.-?\d+$")
SCOPES = {"main", "extra", "deck"}

# ========================================
# REPORTING
# ========================================

@dataclass
class Finding:
    level   : str
    code    : str
    path    : str
    line    : Optional[int]
    message : str


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)

    def add(self, level: str, code: str, path: str, line: Optional[int], message: str) -> None:
        self.findings.append(Finding(level, code, path, line, message))

    def error(self, code: str, path: str, line: Optional[int], message: str) -> None:
        self.add("error", code, path, line, message)

    def warn(self, code: str, path: str, line: Optional[int], message: str) -> None:
        self.add("warning", code, path, line, message)

    def note(self, code: str, path: str, line: Optional[int], message: str) -> None:
        self.add("note", code, path, line, message)

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "warning"]

    @property
    def notes(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "note"]


# ========================================
# PARSING HELPERS
# ========================================

@dataclass
class ManifestRow:
    line    : int
    values  : Dict[str, str]
    period  : Optional[str] = None
    deck    : Optional["Deck"] = None

    def get(self, column: str) -> str:
        return self.values.get(column, "").strip()

@dataclass
class DeckTypeRow:
    line            : int
    deck_type       : str
    aliases         : List[str]
    defining_cards  : str
    description     : str
    kind            : str


@dataclass
class Deck:
    main: List[int] = field(default_factory=list)
    extra: List[int] = field(default_factory=list)
    side: List[int] = field(default_factory=list)

    def all_ids(self) -> List[int]:
        return self.main + self.extra + self.side


def read_bytes(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def check_text_mechanics(report: Report, rel: str, raw: bytes, *, require_bom: bool) -> None:
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if has_bom else raw

    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        report.error("F001", rel, None, f"not valid UTF-8: {exc}")
        return

    if require_bom and not has_bom:
        report.warn("F002", rel, None, "no UTF-8 BOM")
    if not require_bom and has_bom:
        report.error("F003", rel, None, "UTF-8 BOM present")

    if b"\r\n" in body:
        report.warn("F004", rel, None, "CRLF line endings; the repository uses LF")
    if b"\r" in body.replace(b"\r\n", b""):
        report.error("F008", rel, None,
                     "bare CR line endings (classic Mac OS); the file reads as a single line. "
                     "tools/fix_tables.py converts them to LF")
    if body and not body.endswith(b"\n"):
        report.error("F005", rel, None, "no trailing newline")
    if body.endswith(b"\n\n"):
        report.error("F006", rel, None, "more than one trailing newline")
    for idx, raw_line in enumerate(body.decode("utf-8", "replace").split("\n"), start=1):
        if raw_line != raw_line.rstrip():
            report.warn("F007", rel, idx, "trailing whitespace")


def parse_lflist(text: str) -> Tuple[Optional[str], Dict[int, int], List[str]]:
    """Return (header line, {passcode: status}, problems)."""
    header = None
    statuses: Dict[int, int] = {}
    problems: List[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("!"):
            if header is None:
                header = line
            continue
        if line.startswith("$") or line.startswith("#"):
            continue

        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].strip("-").isdigit():
            problems.append(line[:60])
            continue

        code, status = int(parts[0]), int(parts[1])
        if code in statuses and statuses[code] != status:
            problems.append(f"duplicate passcode {code} with conflicting status")
        statuses[code] = status

    return header, statuses, problems


def parse_lflist_comments(text: str) -> Dict[int, str]:
    """Return {passcode: comment}; the comment is the card name in upper case."""
    comments: Dict[int, str] = {}
    for raw_line in text.splitlines():
        match = re.match(r"^\s*(\d+)\s+-?\d+\s*--\s*(.*?)\s*$", raw_line)
        if match:
            comments[int(match.group(1))] = match.group(2)
    return comments


def norm_name(name: str) -> str:
    """Card name normalised for comparison with list comments (case, accents, punctuation)."""
    import unicodedata
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z]+", "", text.casefold())


def parse_ydk(text: str) -> Tuple[Deck, List[Tuple[int, str]]]:
    """Return (deck, [(line_number, problem)])."""
    deck = Deck()
    problems: List[Tuple[int, str]] = []
    section: Optional[str] = None
    for idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("#main"):
            section = "main"
            continue
        if low.startswith("#extra"):
            section = "extra"
            continue
        if low.startswith("!side"):
            section = "side"
            continue
        if line.startswith("#") or line.startswith("!"):
            continue  # "#created by ..." and similar comments
        if not line.isdigit():
            problems.append((idx, f"not a passcode: {line[:40]!r}"))
            continue
        if section is None:
            problems.append((idx, f"passcode {line} before any section header"))
            continue

        getattr(deck, section).append(int(line))

    return deck, problems


# ========================================
# DECK_TYPES.CSV
# ========================================

class GrammarError(ValueError):
    pass


def split_top(text: str, sep: str) -> List[str]:
    """Split on sep at bracket depth 0."""
    out, depth, buf = [], 0, []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise GrammarError("unbalanced brackets")

        if ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)

    if depth != 0:
        raise GrammarError("unbalanced brackets")
    out.append("".join(buf))
    return out


def parse_term(term: str, passcodes: Set[int]) -> None:
    term = term.strip()
    if not term:
        raise GrammarError("empty term")
    if term.startswith("(") and term.endswith(")"):
        inner = term[1:-1]
        if not inner.strip():
            raise GrammarError("empty bracket group")
        for element in split_top(inner, "|"):
            for sub in split_top(element, "&"):
                parse_term(sub, passcodes)
        return

    if term.isdigit():
        passcodes.add(int(term))
        return
    if ":" not in term:
        raise GrammarError(f"term {term!r} is neither a passcode nor <field>:<value>")

    field_name, _, value = term.partition(":")
    field_name = field_name.strip().lower()
    value = value.strip()
    if not value:
        raise GrammarError(f"term {term!r} has an empty value")
    if field_name == "arch":
        return
    if field_name not in ALL_FIELDS:
        raise GrammarError(f"unknown field {field_name!r} in {term!r}")
    if field_name in NUMERIC_FIELDS:
        if not NUMERIC_VALUE_RE.match(value):
            raise GrammarError(f"{term!r}: value must be an integer, a comparison or a range")
    elif field_name in ENUM_FIELDS and value.lower() not in ENUM_FIELDS[field_name]:
        raise GrammarError(f"{term!r}: {value!r} is not a valid {field_name} value")


def parse_defining_cards(expr: str) -> Set[int]:
    """Validate against deck_types grammar rules and return the passcodes referenced."""
    passcodes: Set[int] = set()
    clauses = split_top(expr, ";")
    if not clauses or all(not c.strip() for c in clauses):
        raise GrammarError("no clause")

    for clause in clauses:
        clause = clause.strip()
        if not clause:
            raise GrammarError("empty clause")

        negated = clause.startswith("!")
        if negated:
            clause = clause[1:].strip()

        scope_match = re.match(r"^(main|extra|deck)\s*:(?![\s]*$)", clause, re.IGNORECASE)
        if scope_match and scope_match.group(1).lower() in SCOPES:
            clause = clause[scope_match.end():].strip()

        quantity = re.match(r"^(\d+)\s*([#*])", clause)
        if quantity:
            if negated:
                raise GrammarError("a '!' clause takes no quantity")
            if int(quantity.group(1)) < 1:
                raise GrammarError("quantity must be a positive integer")
            clause = clause[quantity.end():].strip()

        if not clause:
            raise GrammarError("clause has no term")
        for element in split_top(clause, "|"):
            if not element.strip():
                raise GrammarError("empty alternative")
            for term in split_top(element, "&"):
                parse_term(term, passcodes)

    return passcodes


def load_deck_types(
        repo: Path,
        report: Report,
        live_statuses: Optional[Dict[int, int]]
) -> Dict[str, DeckTypeRow]:
    rel = "deck_types.csv"
    path = repo / rel
    if not path.exists():
        report.error("T000", rel, None, "file missing")
        return {}

    raw = read_bytes(path)
    check_text_mechanics(report, rel, raw, require_bom=False)
    text = raw.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        report.error("T001", rel, None, "file is empty")
        return {}
    if rows[0] != DECK_TYPES_COLUMNS:
        report.error("T001", rel, 1,
                     f"header must be exactly {','.join(DECK_TYPES_COLUMNS)}; found "
                     f"{','.join(rows[0])}")

    by_key: Dict[str, DeckTypeRow] = {}
    alias_owner: Dict[str, str] = {}
    seen_rows: Set[Tuple[str, ...]] = set()
    order: List[str] = []
    for idx, values in enumerate(rows[1:], start=2):
        if not values:
            report.error("T002", rel, idx, "blank line")
            continue
        if len(values) != len(DECK_TYPES_COLUMNS):
            report.error("T003", rel, idx,
                         f"{len(values)} fields, expected {len(DECK_TYPES_COLUMNS)}")
            continue

        tup = tuple(values)
        if tup in seen_rows:
            report.error("T004", rel, idx, "duplicate row")
        seen_rows.add(tup)

        deck_type, aliases_raw, defining, description, kind = [v.strip() for v in values]
        if not deck_type:
            report.error("T005", rel, idx, "deck_type is empty")
            continue
        if deck_type != values[0]:
            report.warn("T006", rel, idx, "deck_type has leading or trailing spaces")
        bad = sorted(set(deck_type) & FORBIDDEN_NAME_CHARS)
        if bad:
            report.error("T007", rel, idx,
                         f"deck_type {deck_type!r} contains reserved characters {''.join(bad)}")

        key = deck_type.lower()
        if key in by_key:
            report.error("T008", rel, idx,
                         f"deck_type {deck_type!r} duplicates line {by_key[key].line}")

        aliases = [a.strip() for a in aliases_raw.split(";") if a.strip()] if aliases_raw else []
        for alias in aliases:
            akey = alias.lower()
            bad = sorted(set(alias) & FORBIDDEN_NAME_CHARS)
            if bad:
                report.error("T009", rel, idx,
                             f"alias {alias!r} contains reserved characters {''.join(bad)}")
            if akey in alias_owner:
                report.error("T010", rel, idx,
                             f"alias {alias!r} is already used by {alias_owner[akey]!r}")
            else:
                alias_owner[akey] = deck_type

        if kind not in DECK_TYPE_KINDS:
            report.error("T011", rel, idx,
                         f"kind {kind!r} is not one of {', '.join(sorted(DECK_TYPE_KINDS))}")

        if not description:
            report.error("T012", rel, idx, "description is empty")
        else:
            if len(description) > 300:
                report.error("T013", rel, idx,
                             f"description is {len(description)} characters, maximum 300")
            if "\n" in description:
                report.error("T014", rel, idx, "description spans more than one line")
            if BARE_PASSCODE_RE.search(description):
                report.error("T015", rel, idx,
                             "description contains a bare passcode; use the card name")

        if not defining:
            report.error("T016", rel, idx, "defining_cards is empty")
        else:
            try:
                passcodes = parse_defining_cards(defining)
            except GrammarError as exc:
                report.error("T017", rel, idx, f"defining_cards does not parse: {exc}")
            else:
                if live_statuses is not None:
                    for code in sorted(passcodes):
                        status = live_statuses.get(code)
                        if status is None:
                            report.error("T018", rel, idx,
                                         f"passcode {code} is not on the current SFFR list")
                        elif status == 0:
                            report.error("T019", rel, idx,
                                         f"passcode {code} is Forbidden on the current SFFR list")

            row = DeckTypeRow(idx, deck_type, aliases, defining, description, kind)
            by_key[key] = row
            order.append(key)

    for alias_key, owner in alias_owner.items():
        if alias_key in by_key:
            report.error("T020", rel, by_key[alias_key].line,
                         f"alias {alias_key!r} of {owner!r} equals a deck_type")

    if order != sorted(order):
        report.warn("T021", rel, None,
                    "rows are not sorted case-insensitively by deck_type")
    if live_statuses is None:
        report.note("T099", rel, None,
                    "passcode checks skipped: no current SFFR list "
                    "supplied with --live-lflist")
    return by_key

# --------------------------------------------------------------------------- #
# periods
# --------------------------------------------------------------------------- #

@dataclass
class Period:
    name: str
    list_path: str
    header: Optional[str]
    statuses: Dict[int, int]
    deck_files: Set[str] = field(default_factory=set)
    comments: Dict[int, str] = field(default_factory=dict)


def scan_periods(repo: Path, report: Report) -> Dict[str, Period]:
    periods: Dict[str, Period] = {}
    decks_dir = repo / "decks"
    if not decks_dir.is_dir():
        report.error("P000", "decks/", None, "directory missing")
        return periods
    for child in sorted(decks_dir.iterdir()):
        rel = f"decks/{child.name}"
        if child.is_file():
            report.error("P001", rel, None,
                         "loose file under decks/; every file belongs to a period folder")
            continue
        if not PERIOD_RE.match(child.name):
            report.error("P002", rel, None,
                         "folder name must be YYYY-MM, or YYYY-MM-DD only when a month "
                         "carries more than one list")
            continue
        list_rel = f"{rel}/sffr.lflist.conf"
        list_path = child / "sffr.lflist.conf"
        header, statuses, problems = None, {}, []
        if not list_path.exists():
            report.error("P003", list_rel, None,
                         "period folder has no sffr.lflist.conf; archived decks are judged "
                         "against the list of their own period")
        else:
            text = read_bytes(list_path).decode("utf-8", "replace")
            header, statuses, problems = parse_lflist(text)
            if header is None:
                report.error("P004", list_rel, None, "no !period header line")
            else:
                match = LIST_HEADER_RE.match(header)
                if not match:
                    report.error("P005", list_rel, 1, f"unreadable header {header!r}")
                elif f"{match.group(1)}-{match.group(2)}" != child.name[:7]:
                    report.error("P006", list_rel, 1,
                                 f"header {header!r} does not match folder {child.name}")
            if not statuses:
                report.error("P007", list_rel, None, "no list entries")
            for problem in problems[:5]:
                report.warn("P008", list_rel, None, f"unparsed list line: {problem}")
        period = Period(child.name, list_rel, header, statuses)
        if list_path.exists():
            period.comments = parse_lflist_comments(read_bytes(list_path).decode("utf-8", "replace"))
        for sub in sorted(child.rglob("*")):
            if sub.is_dir():
                report.error("P009", f"{rel}/{sub.name}", None,
                             "period folders hold no subfolders")
                continue
            name = sub.relative_to(child).as_posix()
            if name == "sffr.lflist.conf":
                continue
            if not name.endswith(".ydk"):
                report.warn("P010", f"{rel}/{name}", None,
                            "unexpected file in a period folder")
                continue
            period.deck_files.add(f"{rel}/{name}")
        periods[child.name] = period
    if not periods:
        report.error("P011", "decks/", None, "no period folders")
    return periods


# --------------------------------------------------------------------------- #
# manifest.csv
# --------------------------------------------------------------------------- #

def load_manifest(repo: Path, report: Report) -> List[ManifestRow]:
    rel = "manifest.csv"
    path = repo / rel
    if not path.exists():
        report.error("M000", rel, None, "file missing")
        return []
    raw = read_bytes(path)
    check_text_mechanics(report, rel, raw, require_bom=True)
    text = raw.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    if not rows:
        report.error("M001", rel, None, "file is empty")
        return []
    if rows[0] != MANIFEST_COLUMNS:
        report.error("M001", rel, 1,
                     "header must be exactly "
                     f"{';'.join(MANIFEST_COLUMNS)}; found {';'.join(rows[0])}")
    out: List[ManifestRow] = []
    for idx, values in enumerate(rows[1:], start=2):
        if not values or all(v == "" for v in values):
            report.error("M002", rel, idx, "blank line")
            continue
        if len(values) != len(MANIFEST_COLUMNS):
            report.error("M003", rel, idx,
                         f"{len(values)} fields, expected {len(MANIFEST_COLUMNS)}. "
                         "A field count that is one too high usually means a trailing ';'; "
                         "one too low means a missing empty field, which silently shifts "
                         "every later value into the wrong column")
            continue
        out.append(ManifestRow(idx, dict(zip(MANIFEST_COLUMNS, values))))
    return out


def parse_int(value: str) -> Optional[int]:
    return int(value) if value.isdigit() else None


def check_rows(rows: Sequence[ManifestRow], repo: Path, periods: Dict[str, Period],
               deck_types: Dict[str, DeckTypeRow], report: Report) -> None:
    rel = "manifest.csv"
    seen_files: Dict[str, int] = {}
    today = dt.date.today()
    for row in rows:
        line = row.line
        file_value = row.get("file")
        kind = row.get("kind")

        # --- file ------------------------------------------------------- #
        if not file_value:
            report.error("M010", rel, line, "file is empty")
        else:
            if file_value in seen_files:
                report.error("M011", rel, line,
                             f"file {file_value} is already registered on line {seen_files[file_value]}")
            seen_files[file_value] = line
            if "\\" in file_value or ".." in file_value.split("/"):
                report.error("M012", rel, line, f"file {file_value!r} is not a plain POSIX path")
            elif not file_value.startswith("decks/") or not file_value.endswith(".ydk"):
                report.error("M013", rel, line,
                             f"file {file_value!r} must be decks/<period>/<name>.ydk")
            elif not (repo / file_value).exists():
                parts = file_value.split("/")
                elsewhere = [p.name for p in periods.values()
                             if f"decks/{p.name}/{parts[-1]}" in p.deck_files]
                hint = (f"; a file of that name exists under {', '.join(elsewhere)}"
                        if elsewhere else "")
                report.error("M014", rel, line, f"file {file_value} does not exist{hint}")
            else:
                row.period = file_value.split("/")[1]

        # --- kind ------------------------------------------------------- #
        if kind not in KINDS:
            report.error("M020", rel, line,
                         f"kind {kind!r} is not one of {', '.join(sorted(KINDS))}")

        # --- date ------------------------------------------------------- #
        date_value = row.get("date")
        date_obj: Optional[dt.date] = None
        if not date_value:
            report.error("M030", rel, line, "date is empty")
        elif not DATE_RE.match(date_value):
            report.error("M031", rel, line, f"date {date_value!r} is not ISO YYYY-MM-DD")
        else:
            try:
                date_obj = dt.date.fromisoformat(date_value)
            except ValueError as exc:
                report.error("M032", rel, line, f"date {date_value!r}: {exc}")
            else:
                if date_obj > today:
                    report.error("M033", rel, line, f"date {date_value} is in the future")

        # --- period ----------------------------------------------------- #
        declared_period = row.get("list_period")
        if declared_period:
            if declared_period not in periods:
                report.error("M040", rel, line,
                             f"list_period {declared_period!r} has no folder under decks/")
            elif row.period and declared_period != row.period:
                report.error("M041", rel, line,
                             f"list_period {declared_period} contradicts the folder "
                             f"{row.period} the file sits in; the folder is the period")
        if row.period and date_obj and row.period[:7] != date_value[:7]:
            report.warn("M042", rel, line,
                        f"date {date_value} falls outside period {row.period}")

        # --- fields by kind --------------------------------------------- #
        event = row.get("event")
        event_type = row.get("event_type")
        placement = row.get("placement")
        field_size = row.get("field_size")
        record = row.get("record")
        pilot = row.get("pilot")
        deck_type = row.get("deck_type")
        engines_raw = row.get("engines")

        if kind == "tournament":
            if not event:
                report.error("M050", rel, line, "tournament entries need an event name")
            if event_type and event_type not in EVENT_TYPES:
                report.error("M051", rel, line,
                             f"event_type {event_type!r} is not one of "
                             f"{', '.join(sorted(EVENT_TYPES))} (empty means {DEFAULT_EVENT_TYPE})")
            if not placement:
                report.error("M052", rel, line, "tournament entries need a placement")
            elif parse_int(placement) is None or int(placement) < 1:
                report.error("M053", rel, line, f"placement {placement!r} is not a positive integer")
            if field_size and (parse_int(field_size) is None or int(field_size) < 2):
                report.error("M054", rel, line, f"field_size {field_size!r} is not a valid count")
            if record and not RECORD_RE.match(record):
                report.error("M055", rel, line,
                             f"record {record!r} is not W-L-D, all three parts required")
            if field_size and placement and parse_int(field_size) and parse_int(placement):
                if int(placement) > int(field_size):
                    report.error("M056", rel, line,
                                 f"placement {placement} exceeds field_size {field_size}")
        else:
            for column, value in (("event", event), ("event_type", event_type),
                                  ("placement", placement), ("field_size", field_size),
                                  ("record", record)):
                if value:
                    report.error("M060", rel, line,
                                 f"{column} is set on a {kind!r} entry; it applies to "
                                 "tournament entries only")

        if kind == "reference":
            for column, value in (("pilot", pilot), ("deck_type", deck_type),
                                  ("engines", engines_raw)):
                if value:
                    report.error("M070", rel, line,
                                 f"{column} is set on a reference entry")
        else:
            if not deck_type:
                report.error("M071", rel, line, "deck_type is required")
            elif deck_type.lower() not in deck_types:
                match = next((r for r in deck_types.values()
                              if deck_type.lower() in [a.lower() for a in r.aliases]), None)
                hint = f"; it is an alias of {match.deck_type!r}, use the canonical name" if match else ""
                report.error("M072", rel, line,
                             f"deck_type {deck_type!r} is not defined in deck_types.csv{hint}")
            elif deck_types[deck_type.lower()].kind == "engine":
                report.error("M073", rel, line,
                             f"deck_type {deck_type!r} has kind 'engine' and may appear only "
                             "in the engines column")

        if engines_raw:
            engines = [e.strip() for e in engines_raw.split(";") if e.strip()]
            if len(engines) != len(set(e.lower() for e in engines)):
                report.error("M080", rel, line, "engines lists the same value twice")
            for engine in engines:
                if engine.lower() == deck_type.lower():
                    report.error("M081", rel, line,
                                 f"engines repeats the row's own deck_type {engine!r}")
                elif engine.lower() not in deck_types:
                    report.error("M082", rel, line,
                                 f"engine {engine!r} is not defined in deck_types.csv")
                elif deck_types[engine.lower()].kind == "deck":
                    report.error("M083", rel, line,
                                 f"engine {engine!r} has kind 'deck' and may appear only as "
                                 "a deck_type")

        notes = row.get("notes")
        if "\n" in notes or "\r" in notes:
            report.error("M090", rel, line, "notes spans more than one line")
        elif len(notes) > 300:
            report.warn("M091", rel, line, f"notes is {len(notes)} characters, over 300")

    # --- orphan deck files ---------------------------------------------- #
    registered = set(seen_files)
    for period in periods.values():
        for deck_file in sorted(period.deck_files - registered):
            report.error("M100", deck_file, None,
                         "deck file has no row in manifest.csv")


# --------------------------------------------------------------------------- #
# per-event checks
# --------------------------------------------------------------------------- #

def double_elim_places(field_size: int) -> Dict[int, int]:
    """Standard double-elimination places and how many players share each."""
    places: Dict[int, int] = {}
    for place in (1, 2, 3, 4):
        if place > field_size:
            return places
        places[place] = 1
    place, group, used_at_size = 5, 2, 0
    while place <= field_size:
        places[place] = min(group, field_size - place + 1)
        place += group
        used_at_size += 1
        if used_at_size == 2:
            group *= 2
            used_at_size = 0
    return places


def check_events(rows: Sequence[ManifestRow], report: Report) -> None:
    rel = "manifest.csv"
    events: Dict[Tuple[str, str], List[ManifestRow]] = {}
    for row in rows:
        if row.get("kind") != "tournament":
            continue
        key = (row.get("event").strip().lower(), row.get("list_period") or (row.period or ""))
        events.setdefault(key, []).append(row)

    for (event_name, period), group in sorted(events.items()):
        lines = [r.line for r in group]
        first = group[0]

        types = {(r.get("event_type") or DEFAULT_EVENT_TYPE) for r in group}
        if len(types) > 1:
            report.error("E001", rel, lines[0],
                         f"event {first.get('event')!r} in {period} has conflicting "
                         f"event_type values: {', '.join(sorted(types))} (lines "
                         f"{', '.join(map(str, lines))})")
        event_type = sorted(types)[0]

        sizes = {r.get("field_size") for r in group if r.get("field_size")}
        if len(sizes) > 1:
            report.error("E002", rel, lines[0],
                         f"event {first.get('event')!r} has conflicting field_size values: "
                         f"{', '.join(sorted(sizes))}")
        field_size = int(sorted(sizes)[0]) if len(sizes) == 1 and sorted(sizes)[0].isdigit() else None

        dates = sorted({r.get("date") for r in group if r.get("date")})
        if len(dates) > 1:
            try:
                span = (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days
            except ValueError:
                span = 0
            if span > 7:
                report.warn("E003", rel, lines[0],
                            f"event {first.get('event')!r} spans {span} days "
                            f"({dates[0]} to {dates[-1]}); is it one event or several?")

        if field_size is not None and len(group) > field_size:
            report.error("E004", rel, lines[0],
                         f"event {first.get('event')!r} has {len(group)} archived entries but "
                         f"field_size {field_size}")

        placements: Dict[int, List[int]] = {}
        for row in group:
            value = row.get("placement")
            if value.isdigit():
                placements.setdefault(int(value), []).append(row.line)

        if event_type == "Double-Elim" and field_size:
            allowed = double_elim_places(field_size)
            for place, at_lines in sorted(placements.items()):
                if place not in allowed:
                    report.error("E010", rel, at_lines[0],
                                 f"placement {place} is not a double-elimination place for a "
                                 f"field of {field_size}; valid places are "
                                 f"{', '.join(map(str, sorted(allowed)))}")
                elif len(at_lines) > allowed[place]:
                    report.error("E011", rel, at_lines[0],
                                 f"{len(at_lines)} entries share place {place}, but a field of "
                                 f"{field_size} has only {allowed[place]} there (lines "
                                 f"{', '.join(map(str, at_lines))})")
        else:
            for place, at_lines in sorted(placements.items()):
                if len(at_lines) > 1:
                    level = report.error if event_type == "Swiss" else report.warn
                    level("E012", rel, at_lines[0],
                          f"{len(at_lines)} entries share place {place} in a "
                          f"{event_type} event (lines {', '.join(map(str, at_lines))})"
                          + ("; Swiss standings are broken by tiebreakers" if event_type == "Swiss"
                             else "; shared standings are possible in a round robin, confirm it"))

        rounds_seen: Dict[int, List[int]] = {}
        wins = losses = 0
        complete = field_size is not None and len(group) == field_size
        for row in group:
            record = row.get("record")
            match = RECORD_RE.match(record) if record else None
            if not match:
                continue
            w, l, d = (int(x) for x in match.groups())
            wins += w
            losses += l
            played = w + l + d
            rounds_seen.setdefault(played, []).append(row.line)
            place = int(row.get("placement")) if row.get("placement").isdigit() else None

            if event_type == "Double-Elim":
                if place == 1 and l > 1:
                    report.error("E020", rel, row.line,
                                 f"record {record}: the winner of a double-elimination event "
                                 "has at most one match loss")
                elif place is not None and place > 1 and l != 2:
                    report.error("E021", rel, row.line,
                                 f"record {record}: an eliminated player in a double-elimination "
                                 f"event has exactly two match losses, not {l}")
                if d:
                    report.warn("E022", rel, row.line,
                                f"record {record}: a drawn match in an elimination bracket is "
                                "unusual, confirm it")
            elif event_type == "Round Robin" and field_size:
                per_cycle = field_size - 1
                if per_cycle and (played % per_cycle or played == 0):
                    report.error("E023", rel, row.line,
                                 f"record {record} is {played} matches; a round robin of "
                                 f"{field_size} players gives {per_cycle} per cycle")
                elif per_cycle and played // per_cycle > 2:
                    report.warn("E024", rel, row.line,
                                f"record {record} implies {played // per_cycle} round-robin cycles")

        if event_type in {"Swiss", "Round Robin"} and len(rounds_seen) > 1:
            detail = "; ".join(f"{n} matches on line(s) {', '.join(map(str, ls))}"
                               for n, ls in sorted(rounds_seen.items()))
            report.error("E030", rel, lines[0],
                         f"entries of {first.get('event')!r} played different numbers of "
                         f"matches: {detail}")
        if event_type == "Swiss" and field_size and rounds_seen:
            played = sorted(rounds_seen)[0]
            usual = max(3, math.ceil(math.log2(field_size)))
            if played > usual + 1:
                report.warn("E031", rel, lines[0],
                            f"{played} Swiss rounds for {field_size} players; "
                            f"{usual} is the usual number. If the event had a top cut, decide "
                            "whether record covers Swiss only and whether placement is the "
                            "standings position or the cut result, and record it in notes")
            if played < math.ceil(math.log2(field_size)):
                report.warn("E032", rel, lines[0],
                            f"{played} Swiss rounds cannot separate {field_size} players")
        if complete and wins != losses:
            report.error("E040", rel, lines[0],
                         f"every entry of {first.get('event')!r} is archived, so wins "
                         f"({wins}) and losses ({losses}) must balance")
        if field_size and len(group) < field_size:
            report.note("E090", rel, lines[0],
                        f"event {first.get('event')!r}: {len(group)} of {field_size} decks "
                        "archived; frequency figures over this event are a sample")


# --------------------------------------------------------------------------- #
# deck files
# --------------------------------------------------------------------------- #

def check_decks(rows: Sequence[ManifestRow], repo: Path, periods: Dict[str, Period],
                report: Report) -> None:
    import hashlib
    unresolved_note = False
    digests: Dict[str, List[str]] = {}
    for row in rows:
        rel = row.get("file")
        if not rel or not (repo / rel).exists():
            continue
        raw = read_bytes(repo / rel)
        digests.setdefault(hashlib.sha256(raw).hexdigest(), []).append(rel)
        text = raw.decode("utf-8", "replace")
        deck, problems = parse_ydk(text)
        row.deck = deck
        for line, problem in problems:
            report.error("D001", rel, line, problem)
        kind = row.get("kind")
        counts = {"Main": len(deck.main), "Extra": len(deck.extra), "Side": len(deck.side)}

        if kind == "reference":
            report.note("D090", rel, None,
                        f"reference entry: section sizes ({counts['Main']}/{counts['Extra']}/"
                        f"{counts['Side']}) and construction limits are not checked")
        else:
            if not 40 <= counts["Main"] <= 60:
                report.error("D010", rel, None,
                             f"Main Deck has {counts['Main']} cards, must be 40 to 60")
            if counts["Extra"] > 15:
                report.error("D011", rel, None, f"Extra Deck has {counts['Extra']} cards, maximum 15")
            if counts["Side"] > 15:
                report.error("D012", rel, None, f"Side Deck has {counts['Side']} cards, maximum 15")

        period = periods.get(row.period) if row.period else None
        if not period or not period.statuses:
            continue
        copies: Dict[int, int] = {}
        for code in deck.all_ids():
            copies[code] = copies.get(code, 0) + 1
        unresolved: List[int] = []
        for code, count in sorted(copies.items()):
            status = period.statuses.get(code)
            if status is None:
                unresolved.append(code)
                continue
            if kind == "reference":
                continue
            if status == 0:
                report.error("D021", rel, None,
                             f"passcode {code} is Forbidden on the {period.name} list")
            elif count > status:
                report.error("D022", rel, None,
                             f"{count} copies of passcode {code}, the {period.name} list allows {status}")
        if unresolved:
            report.warn("D020", rel, None,
                        f"{len(unresolved)} passcode(s) are not on the {period.name} list: "
                        f"{', '.join(map(str, unresolved))}. Most are alternate artworks, which "
                        "only the card pool resolves; a passcode that is neither an alternate "
                        "artwork nor listed is an illegal card")

    for rel, previous in redundant_staples(repo, periods).items():
        report.warn("D031", rel, None,
                    f"identical to {previous}, the staples collection already in force; a period "
                    f"folder holds {STAPLES_FILE} only when the staples changed. "
                    "tools/fix_tables.py removes the copy and its manifest row")

    for digest, files in sorted(digests.items()):
        if len(files) > 1 and not any(f.endswith("/" + STAPLES_FILE) for f in files):
            periods_involved = {f.split("/")[1] for f in files}
            level = report.warn if len(periods_involved) > 1 else report.error
            level("D030", files[0], None,
                  "identical deck file registered more than once: "
                  f"{', '.join(files)}"
                  + ("; a file copied across periods misrepresents the later or earlier one"
                     if len(periods_involved) > 1 else ""))


# --------------------------------------------------------------------------- #
# CSV tables: shared reading (also used by fix_tables.py and build_bundle.py)
# --------------------------------------------------------------------------- #

@dataclass
class TableSpec:
    name        : str
    columns     : List[str]
    delimiter   : str
    require_bom : bool
    prefix      : str                        # check-code prefix of the file
    sort_key    : Optional[object] = None    # callable(values dict) -> key, or None


def _restriction_sort_key(values: Dict[str, str]):
    card_id = values.get("card_id", "").strip()
    return (values.get("list_period", "").strip(), values.get("card_name", "").strip().casefold(),
            int(card_id) if card_id.isdigit() else -1)


def _verdict_sort_key(values: Dict[str, str]):
    vid = values.get("verdict_id", "").strip()
    return (int(vid[1:]) if VERDICT_ID_RE.match(vid) else 10 ** 9, vid)


def _deck_type_sort_key(values: Dict[str, str]):
    return values.get("deck_type", "").strip().lower()


TABLE_SPECS: Dict[str, TableSpec] = {
    "manifest.csv": TableSpec("manifest.csv", MANIFEST_COLUMNS, ";", True, "M"),
    "deck_types.csv": TableSpec("deck_types.csv", DECK_TYPES_COLUMNS, ",", False, "T",
                                _deck_type_sort_key),
    "restrictions.csv": TableSpec("restrictions.csv", RESTRICTION_COLUMNS, ",", False, "R",
                                  _restriction_sort_key),
    "verdicts.csv": TableSpec("verdicts.csv", VERDICT_COLUMNS, ",", False, "V",
                              _verdict_sort_key),
}


@dataclass
class Record:
    line   : int              # first physical line of the record (1-based)
    raw    : str              # the record's text, without its final newline
    values : List[str]


def normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_records(text: str, delimiter: str) -> List[Record]:
    """Split CSV text into records, keeping each record's raw text so a fix can touch
    one record without re-quoting the others. `text` must already use LF."""
    lines = text.split("\n")
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    out: List[Record] = []
    previous = 0
    for values in reader:
        end = reader.line_num
        out.append(Record(previous + 1, "\n".join(lines[previous:end]), values))
        previous = end
    return out


def table_rows(spec: TableSpec, text: str) -> Tuple[List[str], List[Tuple[int, Dict[str, str], int]]]:
    """(header, [(line, values padded to the header, raw field count)]); blank records skipped."""
    records = split_records(normalise_newlines(text.lstrip("\ufeff")), spec.delimiter)
    if not records:
        return [], []
    header = [h.strip() for h in records[0].values]
    rows = []
    for rec in records[1:]:
        if not rec.values or all(not v.strip() for v in rec.values):
            continue
        padded = rec.values + [""] * (len(header) - len(rec.values))
        rows.append((rec.line, dict(zip(header, padded)), len(rec.values)))
    return header, rows


def read_table(repo: Path, spec: TableSpec, report: Report
               ) -> Optional[List[Tuple[int, Dict[str, str]]]]:
    """Read one table with the checks every table shares. None when the file is absent."""
    rel = spec.name
    path = repo / rel
    if not path.exists():
        return None
    # verdicts.csv uses V002 and V004 for its own checks
    blank_code = "V014" if spec.prefix == "V" else f"{spec.prefix}002"
    sort_code = "V013" if spec.prefix == "V" else f"{spec.prefix}004"
    raw = read_bytes(path)
    check_text_mechanics(report, rel, raw, require_bom=spec.require_bom)
    text = normalise_newlines(raw.decode("utf-8-sig", "replace"))
    records = split_records(text, spec.delimiter)
    if not records:
        report.error(f"{spec.prefix}001", rel, None, "file is empty")
        return []
    header = [h.strip() for h in records[0].values]
    if header != spec.columns:
        report.error(f"{spec.prefix}001", rel, 1,
                     f"header must be exactly {spec.delimiter.join(spec.columns)}; found "
                     f"{spec.delimiter.join(header)}")
    out: List[Tuple[int, Dict[str, str]]] = []
    seen: Dict[Tuple[str, ...], int] = {}
    for rec in records[1:]:
        if not rec.values or all(not v.strip() for v in rec.values):
            report.error(blank_code, rel, rec.line, "blank line")
            continue
        if len(rec.values) != len(spec.columns):
            extra_empty = (len(rec.values) > len(spec.columns)
                           and all(not v.strip() for v in rec.values[len(spec.columns):]))
            fixable = len(rec.values) < len(spec.columns) or extra_empty
            report.error(f"{spec.prefix}001", rel, rec.line,
                         f"{len(rec.values)} fields, expected {len(spec.columns)}"
                         + ("; tools/fix_tables.py repairs this (missing or surplus empty "
                            "trailing fields)" if fixable else
                            "; the surplus fields are not empty, so a value is probably "
                            "unquoted or misplaced"))
            if not fixable:
                continue
        values = [v.strip() for v in rec.values[:len(spec.columns)]]
        values += [""] * (len(spec.columns) - len(values))
        key = tuple(values)
        if key in seen:
            report.error(blank_code, rel, rec.line,
                         f"duplicate of the row on line {seen[key]}")
        seen.setdefault(key, rec.line)
        out.append((rec.line, dict(zip(spec.columns, values))))
    if spec.sort_key and [spec.sort_key(v) for _, v in out] != sorted(spec.sort_key(v) for _, v in out):
        report.warn(sort_code, rel, None,
                    "rows are not in the documented sort order; tools/fix_tables.py sorts them")
    return out


# --------------------------------------------------------------------------- #
# categories.md
# --------------------------------------------------------------------------- #

@dataclass
class Category:
    label      : str
    basis      : str
    definition : str


def parse_categories(text: str) -> Tuple[List[Category], List[Tuple[int, str]]]:
    """Read the first Markdown table whose first column holds the labels."""
    out: List[Category] = []
    problems: List[Tuple[int, str]] = []
    in_table = False
    for idx, line in enumerate(normalise_newlines(text.lstrip("\ufeff")).split("\n"), start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table and out:
                break
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells if c):
            in_table = True
            continue
        if not in_table:
            continue                             # the header row
        if len(cells) < 3:
            problems.append((idx, f"row has {len(cells)} cells, expected 3"))
            continue
        label = cells[0].strip("`* ")
        basis = cells[1].strip("* ")
        definition = "|".join(cells[2:]).strip()
        out.append(Category(label, basis, definition))
    return out, problems


def load_categories(repo: Path, report: Report, needed: bool) -> Optional[Set[str]]:
    rel = CATEGORIES_FILE
    path = repo / rel
    if not path.exists():
        if needed:
            report.error("C000", rel, None,
                         "file missing; restrictions.csv and verdicts.csv take their category "
                         "vocabulary from it, so category values cannot be checked")
        return None
    raw = read_bytes(path)
    check_text_mechanics(report, rel, raw, require_bom=False)
    categories, problems = parse_categories(raw.decode("utf-8-sig", "replace"))
    for line, problem in problems:
        report.error("C001", rel, line, problem)
    if not categories:
        report.error("C001", rel, None, "no category table found (| label | basis | definition |)")
        return None
    labels: Set[str] = set()
    for cat in categories:
        if not CATEGORY_LABEL_RE.match(cat.label):
            report.error("C002", rel, None,
                         f"label {cat.label!r} must be lower case letters, digits and hyphens")
        if cat.label in labels:
            report.error("C003", rel, None, f"label {cat.label!r} is defined twice")
        if not cat.definition:
            report.error("C004", rel, None, f"label {cat.label!r} has no definition")
        labels.add(cat.label)
    if "unknown" not in labels:
        report.warn("C005", rel, None,
                    "no 'unknown' label; a row whose reason was never recorded has no honest value")
    return labels


# --------------------------------------------------------------------------- #
# restrictions.csv
# --------------------------------------------------------------------------- #

def current_period(periods: Dict[str, "Period"], live_header: Optional[str]) -> Optional[str]:
    """The live list's period when known, else the latest archived period."""
    if live_header:
        match = LIST_HEADER_RE.match(live_header)
        if match:
            return f"{match.group(1)}-{match.group(2)}"
    return max(periods) if periods else None


def list_status_text(statuses: Dict[int, int], code: int) -> str:
    return "absent" if code not in statuses else str(statuses[code])


def _name_suggestion(period: "Period", name: str, own: int) -> Optional[int]:
    wanted = norm_name(name)
    hits = [code for code, comment in period.comments.items()
            if code != own and norm_name(comment) == wanted]
    return hits[0] if len(hits) == 1 else None


def check_restrictions(repo: Path, report: Report, periods: Dict[str, "Period"],
                       live_statuses: Optional[Dict[int, int]], live_header: Optional[str],
                       categories: Optional[Set[str]]) -> None:
    spec = TABLE_SPECS["restrictions.csv"]
    rel = spec.name
    rows = read_table(repo, spec, report)
    if rows is None:
        report.note("R000", rel, None, "file absent: no restriction is explained")
        return

    by_key: Dict[Tuple[int, str], List[Tuple[int, Dict[str, str]]]] = {}
    for line, v in rows:
        card_id, period_name = v["card_id"], v["list_period"]
        if not card_id.isdigit():
            report.error("R010", rel, line, f"card_id {card_id!r} is not a passcode")
            continue
        if not PERIOD_RE.match(period_name):
            report.error("R011", rel, line, f"list_period {period_name!r} is not YYYY-MM[-DD]")
            continue
        code = int(card_id)
        by_key.setdefault((code, period_name), []).append((line, v))

        if v["status"] not in RESTRICTION_STATUSES:
            report.error("R012", rel, line,
                         f"status {v['status']!r} is not one of 0, 1, 2, 3, absent")
        if v["evidence"] not in EVIDENCE_VALUES:
            report.error("R012", rel, line, f"evidence {v['evidence']!r} is not one of "
                         f"{', '.join(sorted(EVIDENCE_VALUES))}")
        if categories is not None and v["category"] not in categories:
            report.error("R012", rel, line,
                         f"category {v['category']!r} is not defined in {CATEGORIES_FILE}; add "
                         "it there or use a defined label")
        for column in ("card_name", "rationale"):
            if not v[column]:
                report.error("R012", rel, line, f"{column} is empty")
            elif "\n" in v[column]:
                report.error("R012", rel, line, f"{column} spans more than one line")
        if v["rationale"].lower() == "unknown" and v["category"] not in ("", "unknown"):
            report.error("R013", rel, line,
                         f"category {v['category']!r} with rationale 'unknown': a ground without a reason")

        period = periods.get(period_name)
        if period is None:
            report.warn("R015", rel, line,
                        f"list_period {period_name} has no folder under decks/, so the row's "
                        "status cannot be checked against its list")
            continue
        comment = period.comments.get(code)
        if comment and v["card_name"] and norm_name(comment) != norm_name(v["card_name"]):
            hint = _name_suggestion(period, v["card_name"], code)
            report.warn("R014", rel, line,
                        f"card_name {v['card_name']!r} but the {period_name} list names "
                        f"{code} {comment!r}"
                        + (f"; {v['card_name']!r} is {hint} on that list, so the passcode is "
                           "probably wrong" if hint else
                           "; list comments are sometimes out of date, check the passcode"))
        listed = list_status_text(period.statuses, code)
        if v["status"] in RESTRICTION_STATUSES and v["status"] != listed:
            # Decisions are issued in batches before the whitelist is updated, so a row that
            # disagrees with its own period's list is taken as right and the list as lagging.
            report.note("R024", rel, line,
                        f"{code} {v['card_name']}: decided status {v['status']}, the "
                        f"{period_name} list file says {listed}; the row is taken as the decision "
                        "and the list as not yet updated")

    for (code, period_name), group in sorted(by_key.items()):
        if len(group) < 2:
            continue
        period = periods.get(period_name)
        detail = []
        for line, v in group:
            hint = _name_suggestion(period, v["card_name"], code) if period else None
            detail.append(f"line {line} ({v['card_name']}"
                          + (f", which is {hint} on the {period_name} list" if hint else "") + ")")
        report.error("R003", rel, group[1][0],
                     f"({code}, {period_name}) appears {len(group)} times: {'; '.join(detail)}. "
                     "Only one row survives in the bundle")

    now = current_period(periods, live_header)
    if now is None:
        return
    statuses = live_statuses
    source = "current SFFR list"
    if statuses is None and now in periods:
        statuses, source = periods[now].statuses, f"{now} list (latest archived period)"
    if statuses is None:
        return
    archived_now = now in periods
    latest: Dict[int, Tuple[int, Dict[str, str]]] = {}
    for (code, period_name), group in sorted(by_key.items(), key=lambda kv: kv[0][1]):
        if period_name <= now:
            latest[code] = group[0]
    level = report.error if archived_now else report.warn
    for code, status in sorted(statuses.items()):
        if status in (0, 1, 2) and code not in latest:
            name = periods[now].comments.get(code, "") if archived_now else ""
            level("R020", rel, None,
                  f"{code} {name} is at status {status} on the {source} and has no row explaining it"
                  + ("" if archived_now else
                     f"; the live list ({now}) is newer than every archived period, so archive "
                     "it under decks/ and add its rows"))
    for code, (line, v) in sorted(latest.items()):
        now_status = list_status_text(statuses, code)
        if v["status"] != now_status and v["list_period"] != now:
            report.warn("R022", rel, line,
                        f"{code} {v['card_name']}: the latest row ({v['list_period']}) says "
                        f"{v['status']}, the {source} says {now_status}; a row for the change is "
                        "missing")


# --------------------------------------------------------------------------- #
# verdicts.csv
# --------------------------------------------------------------------------- #

def check_verdicts(repo: Path, report: Report, periods: Dict[str, "Period"],
                   categories: Optional[Set[str]]) -> None:
    spec = TABLE_SPECS["verdicts.csv"]
    rel = spec.name
    rows = read_table(repo, spec, report)
    if rows is None:
        report.note("V000", rel, None, "file absent: no verdict is recorded")
        return
    today = dt.date.today()
    ids: Dict[str, int] = {}
    decided: Dict[str, str] = {}
    links: Dict[str, Dict[str, str]] = {}
    by_cards: Dict[Tuple[str, ...], List[Tuple[int, Dict[str, str]]]] = {}
    for line, v in rows:
        vid = v["verdict_id"]
        if not VERDICT_ID_RE.match(vid):
            report.error("V002", rel, line, f"verdict_id {vid!r} is not V followed by 3+ digits")
        elif vid in ids:
            report.error("V002", rel, line, f"verdict_id {vid} is already used on line {ids[vid]}")
        ids.setdefault(vid, line)
        decided[vid] = v["verdict"]
        links[vid] = {"supersedes": v["supersedes"], "variant_of": v["variant_of"], "line": str(line)}

        if not DATE_RE.match(v["date"]):
            report.error("V003", rel, line, f"date {v['date']!r} is not ISO YYYY-MM-DD")
        else:
            try:
                if dt.date.fromisoformat(v["date"]) > today:
                    report.error("V003", rel, line, f"date {v['date']} is in the future")
            except ValueError as exc:
                report.error("V003", rel, line, f"date {v['date']!r}: {exc}")
        if not PERIOD_RE.match(v["list_period"]):
            report.error("V003", rel, line, f"list_period {v['list_period']!r} is not YYYY-MM[-DD]")
        elif v["list_period"] not in periods:
            report.warn("V003", rel, line,
                        f"list_period {v['list_period']} has no folder under decks/")

        if v["verdict"] not in VERDICT_VALUES:
            report.error("V004", rel, line,
                         f"verdict {v['verdict']!r} is not a copy limit (0 = not eligible, 1, 2, 3)")
        for column, allowed in (("confidence", CONFIDENCE_VALUES), ("authority", AUTHORITY_VALUES),
                                ("subject_kind", SUBJECT_KINDS)):
            if v[column] not in allowed:
                report.error("V004", rel, line, f"{column} {v[column]!r} is not one of "
                             f"{', '.join(sorted(allowed))}")
        if v["category"]:
            if categories is not None and v["category"] not in categories:
                report.error("V011", rel, line,
                             f"category {v['category']!r} is not defined in {CATEGORIES_FILE}")
        elif v["verdict"] in ("0", "1", "2"):
            report.error("V011", rel, line,
                         f"verdict {v['verdict']} restricts the card but category is empty; "
                         "use 'unknown' if the ground is not known")
        for column in ("subject", "grounds"):
            if not v[column]:
                report.error("V005", rel, line, f"{column} is empty")
            elif "\n" in v[column]:
                report.error("V005", rel, line, f"{column} spans more than one line")

        for column in ("card_ids", "anchors"):
            if v[column] and not ID_LIST_RE.match(v[column]):
                report.error("V006", rel, line,
                             f"{column} {v[column]!r} must be passcodes separated by ';'")
        card_ids = [int(c) for c in v["card_ids"].split(";") if c.isdigit()]
        if not card_ids and v["subject_kind"] != "proposal":
            report.error("V006", rel, line,
                         "card_ids is empty; only a proposal that is in no database has no passcode")
        if v["subject_kind"] == "package" and len(card_ids) < 2:
            report.error("V007", rel, line, "a package verdict names at least two card_ids")

        period = periods.get(v["list_period"])
        if period and len(card_ids) == 1 and v["subject"]:
            comment = period.comments.get(card_ids[0])
            if comment and norm_name(comment) != norm_name(v["subject"]):
                hint = _name_suggestion(period, v["subject"], card_ids[0])
                report.warn("V012", rel, line,
                            f"subject {v['subject']!r} but the {v['list_period']} list names "
                            f"{card_ids[0]} {comment!r}"
                            + (f"; {v['subject']!r} is {hint} on that list" if hint else ""))
        if card_ids:
            by_cards.setdefault(tuple(sorted(card_ids)), []).append((line, v))

    for vid, link in links.items():
        line = int(link["line"])
        for column in ("supersedes", "variant_of"):
            ref = link[column]
            if not ref:
                continue
            if ref == vid:
                report.error("V008", rel, line, f"{column} points at the row itself")
            elif ref not in ids:
                report.error("V008", rel, line, f"{column} {ref} names no recorded verdict")
        ref = link["variant_of"]
        if ref and ref in decided and decided[ref] != "0":
            report.warn("V009", rel, line,
                        f"variant_of {ref}, but {ref} is not a rejection (verdict {decided[ref]})")
    for vid in links:                                   # cycles through supersedes
        seen, cur = set(), vid
        while cur and cur in links and cur not in seen:
            seen.add(cur)
            cur = links[cur]["supersedes"]
        if cur == vid:
            report.error("V008", rel, int(links[vid]["line"]), f"supersedes chain from {vid} is a cycle")
    superseded = {link["supersedes"] for link in links.values() if link["supersedes"]}
    for cards, group in sorted(by_cards.items()):
        live = [(line, v) for line, v in group if v["verdict_id"] not in superseded]
        verdicts = {v["verdict"] for _, v in live}
        if len(verdicts) > 1:
            report.warn("V010", rel, live[-1][0],
                        f"cards {';'.join(map(str, cards))} carry different verdicts "
                        f"({', '.join(sorted(verdicts))}) and neither supersedes the other: "
                        + ", ".join(v["verdict_id"] for _, v in live))


# --------------------------------------------------------------------------- #
# Staples.ydk: one copy per change, inherited by later periods
# --------------------------------------------------------------------------- #

def staples_content(path: Path) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """(Main+Side, Extra) as sorted passcodes. Main and Side placement carries no meaning
    in the staples collection, so the two sections compare as one."""
    deck, _ = parse_ydk(read_bytes(path).decode("utf-8", "replace"))
    return tuple(sorted(deck.main + deck.side)), tuple(sorted(deck.extra))


def staples_by_period(periods: Dict[str, "Period"]) -> Dict[str, str]:
    """Every archived period -> the Staples.ydk in force for it: the one in its own folder,
    else the nearest earlier folder that has one. Periods before the first copy are absent."""
    out: Dict[str, str] = {}
    current: Optional[str] = None
    for name in sorted(periods):
        candidate = f"decks/{name}/{STAPLES_FILE}"
        if candidate in periods[name].deck_files:
            current = candidate
        if current:
            out[name] = current
    return out


def redundant_staples(repo: Path, periods: Dict[str, "Period"]) -> Dict[str, str]:
    """{Staples.ydk that repeats the collection in force before it: that earlier file}."""
    out: Dict[str, str] = {}
    previous: Optional[str] = None
    for name in sorted(periods):
        rel = f"decks/{name}/{STAPLES_FILE}"
        if rel not in periods[name].deck_files:
            continue
        if previous and staples_content(repo / rel) == staples_content(repo / previous):
            out[rel] = previous
            continue                          # the earlier copy stays the one in force
        previous = rel
    return out


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def emit(report: Report, args: argparse.Namespace) -> int:
    order = {"error": 0, "warning": 1, "note": 2}
    findings = sorted(report.findings, key=lambda f: (order[f.level], f.path, f.line or 0, f.code))
    for finding in findings:
        where = f"{finding.path}:{finding.line}" if finding.line else finding.path
        print(f"{finding.level.upper():7} {finding.code}  {where}  {finding.message}")
        if args.github and finding.level in {"error", "warning"}:
            line = f",line={finding.line}" if finding.line else ""
            text = finding.message.replace("\n", " ")
            print(f"::{finding.level} file={finding.path}{line},title={finding.code}::{text}")

    summary = (f"{len(report.errors)} error(s), {len(report.warnings)} warning(s), "
               f"{len(report.notes)} note(s)")
    print(f"\nvalidate_meta {VALIDATOR_VERSION}: {summary}")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(f"## sffr-meta validation\n\n**{summary}**\n\n")
            if findings:
                fh.write("| Level | Check | Location | Message |\n|---|---|---|---|\n")
                for f in findings:
                    where = f"`{f.path}`" + (f" line {f.line}" if f.line else "")
                    fh.write(f"| {f.level} | `{f.code}` | {where} | {f.message} |\n")

    if args.json:
        payload = {
            "validator_version": VALIDATOR_VERSION,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "errors": len(report.errors),
            "warnings": len(report.warnings),
            "findings": [vars(f) for f in findings],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    if report.errors:
        return 1
    if args.strict and report.warnings:
        print("--strict: warnings are treated as failures")
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the sffr-meta repository.")
    parser.add_argument("--repo", default=".", help="repository root (default: .)")
    parser.add_argument("--live-lflist",
                        help="path to the current lflists/sffr.lflist.conf of the format "
                             "repository")
    parser.add_argument("--json", help="write the findings to this file as JSON")
    parser.add_argument("--strict", action="store_true", help="fail on warnings too")
    parser.add_argument("--github", action="store_true",
                        help="emit GitHub Actions annotations (default when GITHUB_ACTIONS is set)")
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        args.github = True

    repo = Path(args.repo)
    if not repo.is_dir():
        print(f"not a directory: {repo}", file=sys.stderr)
        return 2

    report = Report()
    live_statuses: Optional[Dict[int, int]] = None
    live_header: Optional[str] = None
    if args.live_lflist:
        live_path = Path(args.live_lflist)
        if live_path.exists():
            live_header, live_statuses, _ = parse_lflist(
                live_path.read_text(encoding="utf-8", errors="replace"))
        else:
            report.warn("T097", str(live_path), None,
                        "current SFFR list not found; passcode checks skipped")

    deck_types = load_deck_types(repo, report, live_statuses)
    periods = scan_periods(repo, report)
    rows = load_manifest(repo, report)
    check_rows(rows, repo, periods, deck_types, report)
    check_events(rows, report)
    check_decks(rows, repo, periods, report)
    needs_categories = (repo / "restrictions.csv").exists() or (repo / "verdicts.csv").exists()
    categories = load_categories(repo, report, needs_categories)
    check_restrictions(repo, report, periods, live_statuses, live_header, categories)
    check_verdicts(repo, report, periods, categories)
    return emit(report, args)


if __name__ == "__main__":
    sys.exit(main())
