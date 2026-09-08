#!/usr/bin/env python3
"""CTA Members Form CSV -> roster YAML.

Workflow:
    1. Open the linked Google Sheet for the form.
    2. File -> Download -> Comma-separated values (.csv).
    3. Drop the file at data/_raw/<filename>.csv (gitignored).
    4. python3 scripts/csv_to_roster.py --csv data/_raw/<filename>.csv

Both modes preserve roster entries whose slug isn't in the CSV.
Difference is how matched entries are reconciled:

  default (merge)   field-level merge per matched slug. Non-empty
                    form fields override existing values; empty form
                    cells preserve existing values; fields the form
                    doesn't ask about (lanl_profile, leadership,
                    years_start, custom photo paths, etc.) are
                    preserved untouched. Safe to run repeatedly.

  --replace         per matched slug, the existing entry is wiped
                    and rewritten wholly from the CSV row. Use when
                    a member resubmits the form intending to clear
                    stale fields. Other roster entries (slugs not in
                    the CSV) are still preserved.

Students and postdocs are two-layer: the roster holds the *latest*
snapshot (drives the profile page); per-year stint files
(students_<year>.yml / postdocs_<year>.yml) hold the *per-year* values
that drive the listing/year cards. For those categories the script also
merges a stint into each active year and bumps <section>_years.yml. A
person's newest year overwrites its stint with the current submission;
older years fill only empty fields (history/hand-edits preserved). The
roster is always overwritten to latest regardless. Active years come
from the per-block "Years in CTA" column (BLOCKS[cat]["years_active"]) when
filled, else fall back to the single `year_joined`. --replace affects rosters only; stint
files always follow the newest-overwrite / older-fill-if-empty rule.
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


COL_TIMESTAMP = 0
COL_RECRUIT_OK = 1
COL_YEAR_JOINED = 2
COL_DISPLAY_NAME = 3
COL_EMAIL = 4
COL_CATEGORY = 5

COL_PHOTO_UPLOAD = 50
COL_PHOTO_LINK = 51
COL_ADDITIONAL = 52

EXPECTED_COLS = 54

# "Years in CTA" multi-year column: a comma-separated list of active years
# (e.g. "2025, 2026") that places a person into each year's stint file. The
# form now collects this once per multi-year category (postdocs, students), so
# the column is *per-block* — its index lives in BLOCKS[category]["years_active"]
# (None for categories the form doesn't ask). active_years() falls back to the
# single `year_joined` value when the cell is empty.

BLOCK_FIELDS_COMMON = [
    ("scholar", 0),
    ("ads", 1),
    ("bio", 2),
    ("interests", 3),
    ("focus", 4),
    ("papers", 5),
]

BLOCKS = {
    "staff": {
        "start": 6,
        "years_active": None,
        "extras": [
            ("university", 6),
            ("role", 7),
            ("division", 8),
            ("group_code", 9),
            ("accepting_students", 10),
        ],
        "out_dir": "staff",
        "out_file": "staff_roster.yml",
        "photo_root": "/staff/images",
    },
    "postdoc": {
        "start": 18,
        "years_active": 17,
        "extras": [
            ("university", 6),
            ("division", 7),
            ("group_code", 8),
            ("mentors", 9),
            ("postdoc_start_year", 10),
            ("postdoc_end_year", 11),
        ],
        "out_dir": "postdocs",
        "out_file": "postdoc_roster.yml",
        "photo_root": "/postdocs/images",
    },
    "student": {
        "start": 31,
        "years_active": 30,
        "extras": [
            ("university", 6),
            ("division", 7),
            ("group_code", 8),
            ("student_level", 9),
        ],
        "out_dir": "students",
        "out_file": "student_roster.yml",
        "photo_root": "/students/images",
    },
    "emeritus": {
        "start": 41,
        "years_active": None,
        "extras": [
            ("former_role", 6),
            ("former_division", 7),
            ("current_position", 8),
        ],
        "out_dir": "emeritus",
        "out_file": "emeritus_roster.yml",
        "photo_root": "/emeritus/images",
    },
    "retired": {
        "start": 41,
        "years_active": None,
        "extras": [
            ("former_role", 6),
            ("former_division", 7),
            ("current_position", 8),
        ],
        "out_dir": "staff",
        "out_file": "staff_roster.yml",
        "photo_root": "/staff/images",
    },
}

LIST_FIELDS = {"interests", "focus", "papers", "mentors"}

# Two-layer categories. Students and postdocs render their listing/year cards
# from per-year "stint" files (students_<year>.yml / postdocs_<year>.yml), not
# the roster. The roster holds the *latest* value (drives the profile page); the
# stint holds the *per-year* value (drives the card). `fields` maps a stint key
# to the roster-entry key it sources from. Postdocs collect no role/level field,
# so postdoc stint `role` stays hand-curated.
STINT_BLOCKS = {
    "student": {
        "section": "students",
        "stint_file": "students_{year}.yml",
        "years_file": "student_years.yml",
        "fields": {
            "role": "student_level",
            "university": "university",
            "division": "division",
            "group_code": "group_code",
            "focus": "focus",
        },
    },
    "postdoc": {
        "section": "postdocs",
        "stint_file": "postdocs_{year}.yml",
        "years_file": "postdoc_years.yml",
        "fields": {
            "university": "university",
            "division": "division",
            "group_code": "group_code",
            "focus": "focus",
        },
    },
}


def slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def sort_key(name: str) -> str:
    parts = name.strip().split()
    if len(parts) < 2:
        return name.strip().lower()
    return f"{parts[-1].lower()}, {' '.join(parts[:-1]).lower()}"


def split_list(value: str) -> list[str]:
    if not value:
        return []
    if "\n" in value:
        items = [v.strip() for v in value.splitlines()]
    elif "," not in value and ";" in value:
        # Some members separate a list with semicolons instead of commas.
        items = [v.strip() for v in value.split(";")]
    else:
        items = [v.strip() for v in value.split(",")]
    return [v for v in items if v]


def classify(category_cell: str) -> str | None:
    s = (category_cell or "").lower()
    # Retired and Emeritus answers both route to staff with status:retired
    # for now; differentiation between the two will be handled via manual
    # data edits until the form distinguishes them properly.
    if "retired" in s or "emeritus" in s or "former" in s:
        return "retired"
    if "postdoc" in s:
        return "postdoc"
    if "student" in s:
        return "student"
    if "staff" in s:
        return "staff"
    return None


def truthy(cell: str) -> bool:
    return (cell or "").strip().lower().startswith(("y", "true", "1"))


GROUP_CODE_DECLINED = {"rather not say", "n/a", "na", "none", "-"}

# The form asks for "Division" and gets the code back; the rosters spell it out.
DIVISION_NAMES = {
    "T": "Theoretical",
    "THEORY": "Theoretical",
    "THEORETICAL": "Theoretical",
    "CAI": "Computing and Artificial Intelligence",
    "CCS": "Computer, Computational & Statistical Sciences",
    "XCP": "Computational Physics",
    "XTD": "Theoretical Design",
    "X THEORETICAL DESIGN": "Theoretical Design",
    "ISR": "Intelligence & Space Research",
    "NSEC": "National Security Education Center",
    "HPC": "High Performance Computing",
}


# The form's level options read as a single degree; the cards label a cohort.
STUDENT_LEVELS = {"Master": "Masters", "Bachelor": "Undergraduate"}


def normalise_student_level(entry: dict[str, Any]) -> None:
    level = (entry.get("student_level") or "").strip()
    if level in STUDENT_LEVELS:
        entry["student_level"] = STUDENT_LEVELS[level]


def normalise_division(entry: dict[str, Any]) -> None:
    """Expand a division code into the spelled-out name the cards render.

    A code typed with its group suffix ("CAI-2", "T-CNLS") also fills group_code
    when the member left that cell empty. Anything already spelled out, or not a
    code we know, is left alone.
    """
    raw = (entry.get("division") or "").strip()
    if not raw:
        return
    if raw.upper() in DIVISION_NAMES:
        entry["division"] = DIVISION_NAMES[raw.upper()]
        return
    head, _, tail = raw.partition("-")
    if tail and head.upper() in DIVISION_NAMES:
        entry["division"] = DIVISION_NAMES[head.upper()]
        if not (entry.get("group_code") or "").strip():
            entry["group_code"] = raw.upper()


def normalise_group_code(entry: dict[str, Any]) -> None:
    """Repair the two ways the form's Division / Group code pair comes back wrong.

    People routinely split the code across both cells ("CAI" + "2"), leaving a
    bare number where the group belongs. Rejoin those into "CAI-2". People who
    decline the question type prose ("Rather not say"), which is not a code.
    """
    code = (entry.get("group_code") or "").strip()
    if not code:
        return
    if code.lower() in GROUP_CODE_DECLINED:
        entry["group_code"] = None
        return
    if code.isdigit():
        division = (entry.get("division") or "").strip()
        # Only a division that is itself a code ("T", "CAI") can prefix a group.
        if division and re.fullmatch(r"[A-Za-z]{1,4}", division):
            entry["group_code"] = f"{division.upper()}-{code}"


def row_to_entry(row: list[str], category: str) -> tuple[dict[str, Any], str | None]:
    block = BLOCKS[category]
    start = block["start"]

    name = row[COL_DISPLAY_NAME].strip()
    slug = slugify(name)

    entry: dict[str, Any] = {
        "slug": slug,
        "display_name": name,
        "sort_key": sort_key(name),
        "email": row[COL_EMAIL].strip(),
        "photo": f"{block['photo_root']}/{slug}.jpg",
    }

    for field, offset in BLOCK_FIELDS_COMMON:
        raw = (row[start + offset] or "").strip()
        entry[field] = split_list(raw) if field in LIST_FIELDS else (raw or None)

    for field, offset in block["extras"]:
        raw = (row[start + offset] or "").strip()
        entry[field] = split_list(raw) if field in LIST_FIELDS else (raw or None)

    normalise_group_code(entry)
    normalise_division(entry)
    normalise_student_level(entry)

    additional = (row[COL_ADDITIONAL] or "").strip()

    # The profile card renders this as "since <year_joined>", so only a year
    # belongs here. Longtimers answer in prose ("from the beginning"); keep
    # what they wrote as a note rather than printing it as a date.
    year = (row[COL_YEAR_JOINED] or "").strip()
    if year:
        found = re.search(r"\b(19|20)\d{2}\b", year)
        if found:
            entry["year_joined"] = found.group(0)
        else:
            additional = f"Year joined, as submitted: {year}" if not additional else \
                f"{additional} | Year joined, as submitted: {year}"

    if additional:
        entry["additional"] = additional

    if truthy(row[COL_RECRUIT_OK]):
        entry["recruiting_ok"] = True

    if category == "retired":
        entry["status"] = "retired"

    photo_url = (row[COL_PHOTO_UPLOAD] or "").strip() or (row[COL_PHOTO_LINK] or "").strip()
    return entry, photo_url or None


def write_roster(rows: list[dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        _dump_list_with_blank_lines(rows, f)


def _dump_list_with_blank_lines(rows: list[Any], f) -> None:
    """Dump a YAML list with one blank line between top-level entries.
    Round-trip safe: yaml.safe_load ignores the extra newlines."""
    if not rows:
        f.write("[]\n")
        return
    chunks = []
    for entry in rows:
        text = yaml.safe_dump(
            [entry],
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=120,
        )
        chunks.append(text.rstrip())
    f.write("\n\n".join(chunks) + "\n")


# Fields whose curated roster value beats the form answer when one already
# exists. New people still get the form value (nothing to preserve yet).
#   papers                  roster holds structured {title, authors, year,
#                           journal, url} entries; the form collects free text.
#   division/former_division  roster spells divisions out ("Computing and
#                           Artificial Intelligence"); members answer with the
#                           code, and normalise_division only knows some of them.
#   university              roster means alma mater; the form question conflates
#                           it with current affiliation, which lives in
#                           `affiliation`.
PRESERVE_IF_EXISTING = {"photo", "sort_key", "papers", "division",
                        "former_division", "university"}


# List questions members routinely answer in sentences. The cards render each
# item as its own chip, so one run-on item is worse than the curated list it
# would replace.
PROSE_GUARDED_LISTS = {"interests", "focus"}


def is_prose_list(value: Any) -> bool:
    """A list answer that is really one sentence, not a list."""
    if not isinstance(value, list) or len(value) != 1:
        return False
    item = str(value[0])
    return len(item) > 80 or ". " in item


def field_merge(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Field-level merge: new value wins when non-empty, except for fields
    in PRESERVE_IF_EXISTING (hand-curated values survive). Fields the form
    didn't ask about (i.e., not present in `new`) are preserved automatically.
    """
    merged = dict(existing)
    for k, v in new.items():
        if v is None or v == "":
            continue
        if isinstance(v, list) and not v:
            continue
        if k in PRESERVE_IF_EXISTING and existing.get(k):
            continue
        if k in PROSE_GUARDED_LISTS and existing.get(k) and is_prose_list(v):
            continue
        merged[k] = v
    return merged


def merge_roster(
    new_rows: list[dict[str, Any]],
    existing_path: Path,
    replace_matched: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (final entries, per-slug actions).

    Each action is a dict: {slug, action, ...details}.
    action ∈ {"added", "merged", "replaced", "preserved"}.
    """
    actions: list[dict[str, Any]] = []
    by_slug: dict[str, dict[str, Any]] = {}

    if existing_path.exists():
        with existing_path.open("r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or []
        for e in existing:
            if isinstance(e, dict) and e.get("slug"):
                by_slug[e["slug"]] = e

    new_slugs = {r["slug"] for r in new_rows}
    for slug in by_slug:
        if slug not in new_slugs:
            actions.append({"slug": slug, "action": "preserved"})

    for row in new_rows:
        slug = row["slug"]
        if slug not in by_slug:
            by_slug[slug] = row
            actions.append({"slug": slug, "action": "added"})
        elif replace_matched:
            old = by_slug[slug]
            removed = sorted(set(old) - set(row))
            by_slug[slug] = row
            actions.append({
                "slug": slug, "action": "replaced", "fields_removed": removed,
            })
        else:
            old = by_slug[slug]
            merged = field_merge(old, row)
            changed = sorted(
                k for k in set(merged) | set(old)
                if old.get(k) != merged.get(k)
            )
            by_slug[slug] = merged
            actions.append({
                "slug": slug, "action": "merged", "fields_changed": changed,
            })

    return list(by_slug.values()), actions


_ACTION_GLYPH = {
    "added": "+",
    "merged": "~",
    "replaced": "=",
    "preserved": "·",
}


def print_summary(path: Path, entries: list[dict[str, Any]],
                  actions: list[dict[str, Any]], dry_run: bool) -> None:
    verb = "would update" if dry_run else "updated"
    counts: dict[str, int] = {}
    for a in actions:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    parts = [f"{n} {k}" for k, n in counts.items()]
    summary = "  ".join(parts) if parts else "no changes"
    print(f"\n{verb} {path}  ({len(entries)} total: {summary})")
    for a in actions:
        slug = a["slug"]
        glyph = _ACTION_GLYPH.get(a["action"], "?")
        if a["action"] == "merged":
            fields = a.get("fields_changed", [])
            detail = f"merged: {', '.join(fields)}" if fields else "merged: no diff"
        elif a["action"] == "replaced":
            removed = a.get("fields_removed", [])
            detail = f"replaced (dropped: {', '.join(removed)})" if removed else "replaced"
        elif a["action"] == "added":
            detail = "new"
        else:
            detail = "kept; not in CSV"
        print(f"  {glyph} {slug:<28}  {detail}")


def year_int(value: Any) -> int | None:
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits) if digits else None


def active_years(row: list[str], entry: dict[str, Any],
                 years_col: int | None) -> list[int]:
    """Years this person should appear in. Prefers the per-block "Years in CTA"
    column (comma list); falls back to the single `year_joined`. Returns sorted ints."""
    years: list[int] = []
    if years_col is not None and len(row) > years_col:
        for y in split_list((row[years_col] or "").strip()):
            yi = year_int(y)
            if yi:
                years.append(yi)
    if not years:
        yi = year_int(entry.get("year_joined"))
        if yi:
            years = [yi]
    return sorted(set(years))


def build_stint(entry: dict[str, Any], category: str) -> dict[str, Any]:
    """The per-year stint row sourced from the roster entry. List fields
    (focus) are flattened to the comma string the cards expect."""
    cfg = STINT_BLOCKS[category]
    stint: dict[str, Any] = {"slug": entry["slug"]}
    for stint_key, roster_key in cfg["fields"].items():
        v = entry.get(roster_key)
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v) if v else None
        if v:
            stint[stint_key] = v
    return stint


def merge_stint_year(
    jobs: list[tuple[dict[str, Any], bool]],
    year_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge stints into one year file. Each job is (stint, is_newest).
    Newest year for a person overwrites their stint fields with the current
    submission; older years fill only empty fields (preserve history/hand-edits).
    PRESERVE_IF_EXISTING applies here too, so a curated division survives the
    division code members type into the form.
    Existing keys the script doesn't manage (card_image, photo_override) survive.
    """
    by_slug: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    if year_path.exists():
        with year_path.open("r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or []
        for e in existing:
            if isinstance(e, dict) and e.get("slug"):
                by_slug[e["slug"]] = e
                order.append(e["slug"])

    actions: list[dict[str, Any]] = []
    for stint, is_newest in jobs:
        slug = stint["slug"]
        if slug not in by_slug:
            by_slug[slug] = dict(stint)
            order.append(slug)
            actions.append({"slug": slug, "action": "added"})
            continue
        cur = by_slug[slug]
        changed: list[str] = []
        for k, v in stint.items():
            if k == "slug":
                continue
            if k in PRESERVE_IF_EXISTING and cur.get(k):
                continue
            if is_newest:
                if cur.get(k) != v:
                    cur[k] = v
                    changed.append(k)
            elif not cur.get(k):
                cur[k] = v
                changed.append(k)
        actions.append({
            "slug": slug,
            "action": "updated" if changed else "kept",
            "fields_changed": changed,
        })

    return [by_slug[s] for s in order], actions


def print_stint_summary(path: Path, entries: list[dict[str, Any]],
                        actions: list[dict[str, Any]], dry_run: bool) -> None:
    verb = "would update" if dry_run else "updated"
    counts: dict[str, int] = {}
    for a in actions:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    parts = [f"{n} {k}" for k, n in counts.items()]
    summary = "  ".join(parts) if parts else "no changes"
    print(f"\n{verb} {path}  ({len(entries)} total: {summary})")
    for a in actions:
        glyph = {"added": "+", "updated": "~", "kept": "·"}.get(a["action"], "?")
        if a["action"] == "updated":
            fields = a.get("fields_changed", [])
            detail = f"set: {', '.join(fields)}" if fields else "no change"
        elif a["action"] == "added":
            detail = "new stint"
        else:
            detail = "kept"
        print(f"  {glyph} {a['slug']:<28}  {detail}")


def bump_years_file(years_path: Path, new_years: set[int],
                    dry_run: bool) -> list[int]:
    """Add any new years to <section>_years.yml (sorted descending). Returns
    the year list that was (or would be) written; no-op if nothing new."""
    existing: list[int] = []
    if years_path.exists():
        with years_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        existing = [y for y in (year_int(y) for y in (data.get("years") or [])) if y]
    merged = sorted(set(existing) | set(new_years), reverse=True)
    added = sorted(set(new_years) - set(existing), reverse=True)
    if added and not dry_run:
        years_path.parent.mkdir(parents=True, exist_ok=True)
        with years_path.open("w", encoding="utf-8") as f:
            f.write("years: [" + ", ".join(str(y) for y in merged) + "]\n")
    if added:
        verb = "would add" if dry_run else "added"
        print(f"\n{years_path}: {verb} {', '.join(map(str, added))}  -> {merged}")
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", required=True, type=Path,
                        help="Path to form responses CSV.")
    parser.add_argument("--out-dir", default=Path("data"), type=Path,
                        help="Repo data/ root (default: ./data).")
    parser.add_argument("--replace", action="store_true",
                        help="For each CSV slug, wipe the matching existing entry and rewrite "
                             "wholly from the CSV row. Entries not in the CSV are still kept. "
                             "Default is field-level merge per matched slug.")
    parser.add_argument("--only", choices=list(BLOCKS),
                        help="Only emit one category.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse + classify, but don't write any YAML.")
    args = parser.parse_args(argv)

    if not args.csv.exists():
        parser.error(f"CSV not found: {args.csv}")

    by_category: dict[str, list[dict[str, Any]]] = {k: [] for k in BLOCKS}
    # (category, year) -> list of (stint, is_newest) for the per-year files
    stint_jobs: dict[tuple[str, int], list[tuple[dict[str, Any], bool]]] = defaultdict(list)
    skipped = 0
    photo_warnings: list[tuple[str, str, str]] = []

    with args.csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            print("ERROR: empty CSV", file=sys.stderr)
            return 1
        if len(header) != EXPECTED_COLS:
            print(f"ERROR: header has {len(header)} cols, expected {EXPECTED_COLS}. "
                  "The form layout changed; the BLOCKS column offsets are now wrong. "
                  "Refusing to parse — update the offsets before re-running.",
                  file=sys.stderr)
            return 1
        for row_num, row in enumerate(reader, start=2):
            if not any(c.strip() for c in row):
                continue
            if len(row) < EXPECTED_COLS:
                row = list(row) + [""] * (EXPECTED_COLS - len(row))
            category = classify(row[COL_CATEGORY])
            if category is None:
                print(f"WARN row {row_num}: unrecognised category "
                      f"{row[COL_CATEGORY]!r}, skipping", file=sys.stderr)
                skipped += 1
                continue
            entry, photo_src = row_to_entry(row, category)
            if photo_src:
                photo_warnings.append((category, entry["slug"], photo_src))
            by_category[category].append(entry)
            if category in STINT_BLOCKS:
                years = active_years(row, entry, BLOCKS[category].get("years_active"))
                if years:
                    newest = max(years)
                    stint = build_stint(entry, category)
                    for y in years:
                        stint_jobs[(category, y)].append((stint, y == newest))

    by_output: dict[Path, list[dict[str, Any]]] = {}
    for cat, entries in by_category.items():
        if args.only and cat != args.only:
            continue
        block = BLOCKS[cat]
        path = args.out_dir / block["out_dir"] / block["out_file"]
        by_output.setdefault(path, []).extend(entries)

    for path, entries in by_output.items():
        entries, actions = merge_roster(entries, path, replace_matched=args.replace)
        touched = any(a["action"] != "preserved" for a in actions)
        if not args.dry_run and touched:
            write_roster(entries, path)
        print_summary(path, entries, actions, dry_run=args.dry_run)

    # Per-year stint files for two-layer categories (students, postdocs).
    # Roster above is the latest snapshot; these drive the listing/year cards.
    years_touched: dict[tuple[str, str], set[int]] = defaultdict(set)
    for (cat, year), jobs in sorted(stint_jobs.items()):
        if args.only and cat != args.only:
            continue
        cfg = STINT_BLOCKS[cat]
        year_path = args.out_dir / cfg["section"] / cfg["stint_file"].format(year=year)
        stints, actions = merge_stint_year(jobs, year_path)
        if not args.dry_run:
            write_roster(stints, year_path)
        print_stint_summary(year_path, stints, actions, dry_run=args.dry_run)
        years_touched[(cfg["section"], cfg["years_file"])].add(year)

    for (section, years_file), yrs in years_touched.items():
        bump_years_file(args.out_dir / section / years_file, yrs, dry_run=args.dry_run)

    if photo_warnings:
        print("\nPhoto sources flagged (download + place at the photo path manually):",
              file=sys.stderr)
        for cat, slug, url in photo_warnings:
            block = BLOCKS[cat]
            target = f"{block['photo_root']}/{slug}.jpg"
            print(f"  {target}\n    src: {url}", file=sys.stderr)

    if skipped:
        print(f"\nSkipped {skipped} row(s) with unrecognised categories.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
