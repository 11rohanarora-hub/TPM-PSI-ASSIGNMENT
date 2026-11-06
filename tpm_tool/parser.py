"""Tolerant tracker reader: Excel (multi-sheet) or CSV -> two clean tables.

Real trackers have banner rows, merged cells, footnotes and different layouts per
sheet, so we *detect* the header row and map columns by meaning, not position.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

import pandas as pd

# (canonical field, regex on lower-cased header). First match wins -> order matters.
FIELD_RULES = [
    ("id", r"^(id|ref|key|ticket|#)$"),
    ("status_tracking", r"tracking status|item status|^state$"),
    ("milestone", r"milestone"),
    ("target", r"target|deadline"),
    ("due", r"\bdue\b|checkpoint|\beta\b|by when"),
    ("project", r"^(project|client|account|customer)$"),
    ("type", r"^(type|category|kind)$"),
    ("description", r"^(description|details|issue|risk|summary)$"),
    ("probability", r"probab|likelihood"),
    ("impact", r"impact|severity"),
    ("owner", r"owner|assignee|responsible|assigned"),
    ("action", r"next action|mitigation|action"),
    ("problem", r"main problem|problem|blocker"),
    ("decision", r"control|decision"),
    ("status", r"^status$|^rag$|health"),
]
ITEM_COLS = ["id", "project", "type", "status", "description", "probability",
             "impact", "owner", "action", "due"]
PROJECT_COLS = ["project", "rag", "milestone", "target", "problem", "decision"]
CROSS_PROJECT = {"both", "all", "cross", "cross-project", "shared"}


class ParseError(ValueError):
    """Raised with a user-friendly message when a file cannot be understood."""


@dataclass
class ParseResult:
    items: pd.DataFrame
    projects: pd.DataFrame
    warnings: list[str] = field(default_factory=list)
    sheets_used: dict[str, str] = field(default_factory=dict)


def _isnan(v) -> bool:
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def _norm(v) -> str:
    return "" if _isnan(v) else re.sub(r"\s+", " ", str(v)).strip().lower()


def _field_for(header) -> str | None:
    h = _norm(header)
    if not h or len(h) > 40:
        return None
    for name, rx in FIELD_RULES:
        if re.search(rx, h):
            return name
    return None


def _find_header(raw: pd.DataFrame, scan: int = 40) -> int | None:
    best, best_n = None, 1  # need >= 2 recognised columns
    for i in range(min(scan, len(raw))):
        n = sum(_field_for(c) is not None for c in raw.iloc[i].tolist() if isinstance(c, str))
        if n > best_n:
            best, best_n = i, n
    return best


def _clean_sheet(raw: pd.DataFrame):
    """Return (kind, DataFrame with canonical columns) or None if sheet is not a table."""
    raw = raw.dropna(how="all").dropna(axis=1, how="all").reset_index(drop=True)
    raw.columns = range(raw.shape[1])
    hdr = _find_header(raw)
    if hdr is None:
        return None
    mapping, seen = {}, set()
    for col_idx, h in enumerate(raw.iloc[hdr].tolist()):
        f = _field_for(h) if isinstance(h, str) else None
        if f and f not in seen:
            mapping[col_idx] = f
            seen.add(f)
    body = raw.iloc[hdr + 1:][list(mapping)].copy()
    body.columns = [mapping[i] for i in mapping]
    min_filled = 3 if len(mapping) >= 3 else 1  # drops footnotes / notes rows
    body = body[body.notna().sum(axis=1) >= min_filled]
    body = body.map(lambda v: "" if _isnan(v) else str(v).strip()).reset_index(drop=True)

    cols = set(body.columns)
    if cols & {"milestone", "target", "problem"} and "id" not in cols:
        if "status" in cols:
            body = body.rename(columns={"status": "rag"})
        for c in PROJECT_COLS:
            if c not in body:
                body[c] = ""
        return "projects", body[PROJECT_COLS]
    if cols & {"description", "id", "action"}:
        if "status_tracking" in cols:
            body = body.drop(columns=["status"], errors="ignore").rename(
                columns={"status_tracking": "status"})
        for c in ITEM_COLS:
            if c not in body:
                body[c] = ""
        return "items", body[ITEM_COLS]
    return None


def _read_ragged_csv(data, sep: str) -> pd.DataFrame:
    """Read every row, padding short ones. (pandas' C parser sizes columns from the first line, so a
    short banner row above the header would otherwise drop or reject the real rows.)"""
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, (bytes, bytearray)) else str(data)
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=sep)]
    width = max((len(r) for r in rows), default=0)
    if width == 0:
        raise ParseError("The file is empty.")
    padded = [[(c.strip() or None) for c in r] + [None] * (width - len(r)) for r in rows]
    return pd.DataFrame(padded, dtype=object)


def _read_any(data, filename: str) -> dict[str, pd.DataFrame]:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    buf = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
    try:
        if ext in {"csv", "tsv", "txt"}:
            return {filename: _read_ragged_csv(buf.read() if hasattr(buf, "read") else buf,
                                               "\t" if ext == "tsv" else ",")}
        if ext in {"xlsx", "xlsm", "xls"}:
            return pd.read_excel(buf, sheet_name=None, header=None)
    except Exception as exc:  # corrupt / password-protected / wrong format
        raise ParseError(f"Could not open '{filename}': {exc}") from exc
    raise ParseError(f"Unsupported file type '.{ext}'. Upload .xlsx, .xls or .csv.")


def client_of(project: str) -> str:
    """'Kavach – Nirmal Finance' -> 'Nirmal Finance'."""
    parts = re.split(r"\s[–—-]\s", project, maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else project


def _first_part(name: str) -> str:
    return re.split(r"\s[–—-]\s", name)[0].strip().casefold()


def _resolve_project(name: str, known: list[str]) -> str:
    n = name.strip()
    if n.casefold() in CROSS_PROJECT:
        return "ALL"
    for k in known:
        if n.casefold() == k.casefold():
            return k
    for k in known:  # "Kavach" -> "Kavach – Nirmal Finance"
        if n and (n.casefold() == _first_part(k) or n.casefold() in k.casefold()):
            return k
    return n or "Unassigned"


def derive_rag(items: pd.DataFrame) -> str:
    """Fallback project health when the file has no summary sheet."""
    if items.empty:
        return "Green"
    if items["impact"].str.lower().eq("critical").any():
        return "Red"
    if (items["probability"].str.lower().eq("observed") | items["impact"].str.lower().eq("high")).any():
        return "Amber"
    return "Green"


def parse_tracker(data, filename: str) -> ParseResult:
    sheets = _read_any(data, filename)
    items_parts, proj_parts, used = [], [], {}
    for sname, raw in sheets.items():
        res = _clean_sheet(raw)
        if res is None:
            continue
        kind, df = res
        used[sname] = kind
        (items_parts if kind == "items" else proj_parts).append(df)
    if not items_parts:
        raise ParseError("No item/risk table found. Expected columns such as Project, "
                         "Description, Probability, Impact, Owner, Next action, Due date.")
    warnings: list[str] = []
    items = pd.concat(items_parts, ignore_index=True)
    if items.empty:
        raise ParseError("The item table was found but has no data rows.")
    projects = (pd.concat(proj_parts, ignore_index=True) if proj_parts
                else pd.DataFrame(columns=PROJECT_COLS))

    known = projects["project"].tolist()
    items["project"] = items["project"].map(lambda p: _resolve_project(p, known))
    blank = items["id"].eq("")
    if blank.any():
        warnings.append(f"{int(blank.sum())} row(s) had no ID; generated ITEM-n IDs.")
        items.loc[blank, "id"] = [f"ITEM-{i + 1}" for i in range(int(blank.sum()))]
    dup = items["id"][items["id"].duplicated()].unique().tolist()
    if dup:
        warnings.append("Duplicate IDs found: " + ", ".join(dup) + ".")
    if items["description"].eq("").all():
        warnings.append("No description column detected; chat and reports will be thin.")

    # projects referenced by items but absent from the summary sheet
    missing = [p for p in items["project"].unique() if p != "ALL" and p not in projects["project"].tolist()]
    if missing:
        rows = [{"project": p, "rag": derive_rag(items[items.project == p]), "milestone": "",
                 "target": "", "problem": "", "decision": ""} for p in missing]
        projects = pd.concat([projects, pd.DataFrame(rows)], ignore_index=True)
        if proj_parts:
            warnings.append("Projects missing from summary sheet (status derived): " + ", ".join(missing))
    projects = projects.reset_index(drop=True)
    for i, r in projects.iterrows():
        if not str(r["rag"]).strip():
            projects.at[i, "rag"] = derive_rag(items[items.project.isin([r["project"], "ALL"])])
    projects["rag"] = projects["rag"].astype(str).str.strip().str.title()
    return ParseResult(items, projects, warnings, used)
