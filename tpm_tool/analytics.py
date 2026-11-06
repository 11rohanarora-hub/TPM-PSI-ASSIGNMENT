"""Scoring, timeline and capacity analytics. Pure pandas/stdlib (no UI imports)."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import pandas as pd
from dateutil import parser as dparser

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_ALIASES = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
                "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
                "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}
_DAY_WORD = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun)"
DAY_RE = re.compile(rf"\b({_DAY_WORD})\b", re.I)
ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
SLASH_RE = re.compile(r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b")
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
LEAVE_RE = re.compile(
    rf"\b([A-Z][a-z]+)\s+is\s+(?:on\s+leave|out|away|unavailable|off)\s+(?:on\s+)?({_DAY_WORD})"
    rf"(?:\s*(?:–|—|-|to|through)\s*({_DAY_WORD}))?", re.I)
GATE_RE = re.compile(r"\bgate\b|go/no-go|block(?:s|ed)?\s+(?:the\s+)?(?:pilot|production|launch|release)|"
                     r"no (?:live )?(?:uat|pilot)|before live uat|do not launch", re.I)
CLOSED = {"closed", "done", "resolved", "complete", "completed", "cancelled", "canceled"}

IMPACT = {"critical": 4, "severe": 4, "high": 3, "medium": 2, "med": 2, "low": 1}
PROB = {"observed": 1.0, "occurred": 1.0, "very high": 0.9, "high": 0.75, "medium": 0.5, "med": 0.5, "low": 0.2}
RAG = {"red": 1.0, "amber": 0.6, "yellow": 0.6, "green": 0.2}

CRITERIA = ["Impact", "Probability", "Urgency (due date)", "Release gate",
            "Project health (RAG)", "Resource contention"]
DEFAULT_ORDER = ["Impact", "Urgency (due date)", "Release gate", "Probability",
                 "Project health (RAG)", "Resource contention"]
IGNORE_FOR_LOAD = {"tpm"}


def day_label(offset) -> str:
    if offset is None or pd.isna(offset):
        return "TBD"
    offset = int(offset)
    if offset < 0:
        return f"{abs(offset)}d overdue"
    return DAY_NAMES[offset % 7] + (f" (+{offset // 7}w)" if offset >= 7 else "")


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def default_anchor(today: date | None = None) -> date:
    """Monday of the reporting week: upcoming Monday if today is Fri-Sun, else this Monday."""
    t = today or date.today()
    return monday_of(t) + timedelta(days=7 if t.weekday() >= 4 else 0)


def _to_24h(h: int, m: str | None, ap: str) -> str:
    h = h % 12 + (12 if ap.lower() == "pm" else 0)
    return f"{h:02d}:{m or '00'}"


def parse_due(text: str, anchor: date) -> dict:
    """Extract deadlines from free text. Offsets are days from `anchor` (a Monday).

    first/last = earliest / latest mention in the text; time = first clock time.
    """
    text = text or ""
    hits: list[tuple[int, int]] = []  # (position, offset)
    for m in DAY_RE.finditer(text):
        hits.append((m.start(), _DAY_ALIASES[m.group(1).lower()]))
    for rx in (ISO_RE, SLASH_RE):
        for m in rx.finditer(text):
            try:
                d = dparser.parse(m.group(1), dayfirst=False).date()
                hits.append((m.start(), (d - anchor).days))
            except (ValueError, OverflowError):
                continue
    hits.sort()
    offsets = [o for _, o in hits]
    t = TIME_RE.search(text)
    time = _to_24h(int(t.group(1)), t.group(2), t.group(3)) if t else ("17:00" if re.search(r"\bEOD\b", text) else None)
    return {"first": offsets[0] if offsets else None, "last": offsets[-1] if offsets else None,
            "time": time, "label": text.strip()}


def parse_owners(text: str) -> list[str]:
    """'Meera + Sana; Arjun if backend implicated' -> ['Meera','Sana','Arjun']."""
    text = re.split(r"\s+(?:if|for|when|unless)\s+", text or "", flags=re.I)[0] if text else ""
    parts = re.split(r"\s*(?:\+|;|,|&|/|\band\b)\s*", text)
    out = []
    for p in parts:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def is_person(owner: str) -> bool:
    return bool(owner) and owner[0].isupper()


def find_leave(texts: list[str]) -> dict[str, tuple[int, int]]:
    leave = {}
    for t in texts:
        for m in LEAVE_RE.finditer(t or ""):
            d1 = _DAY_ALIASES[m.group(2).lower()]
            d2 = _DAY_ALIASES[m.group(3).lower()] if m.group(3) else d1
            leave[m.group(1)] = (d1, d2)
    return leave


def weights_from_order(order: list[str]) -> dict[str, float]:
    """Drag-order of criteria (top = most important) -> normalised weights."""
    order = list(dict.fromkeys(c for c in order if c in CRITERIA))  # dedupe, keep first occurrence
    order += [c for c in CRITERIA if c not in order]
    raw = {c: len(order) - i for i, c in enumerate(order)}
    tot = sum(raw.values())
    return {c: v / tot for c, v in raw.items()}


def _lookup(table: dict, value: str, default: float) -> float:
    return table.get(str(value).strip().lower(), default)


def enrich(items: pd.DataFrame, projects: pd.DataFrame, weights: dict[str, float] | None = None,
           anchor: date | None = None) -> pd.DataFrame:
    """Add parsed due dates, flags, factor scores, 0-100 priority score and rank."""
    anchor = anchor or default_anchor()
    w = weights or weights_from_order(DEFAULT_ORDER)
    df = items.copy().reset_index(drop=True)
    if df.empty:
        return df
    due = [parse_due(f"{d}", anchor) for d in df["due"]]
    # object dtype keeps real ints/None (a float column with NaN would break `is None` checks)
    df["due_first"] = pd.Series([d["first"] for d in due], dtype=object)
    df["due_last"] = pd.Series([d["last"] for d in due], dtype=object)
    df["due_time"] = pd.Series([d["time"] for d in due], dtype=object)
    df["owners"] = df["owner"].map(parse_owners)
    df["open"] = ~df["status"].str.lower().isin(CLOSED)
    blob = df["description"] + " " + df["action"] + " " + df["due"]
    df["gate"] = blob.map(lambda t: bool(GATE_RE.search(t)))
    df["impact_n"] = df["impact"].map(lambda v: _lookup(IMPACT, v, 2))
    df["prob_n"] = df["probability"].map(lambda v: _lookup(PROB, v, 0.5))
    rag_by_proj = dict(zip(projects["project"], projects["rag"]))
    worst = max((_lookup(RAG, r, 0.2) for r in projects["rag"]), default=0.2)
    shared = " + ".join(re.split(r"\s[–—-]\s", p)[0].strip() for p in projects["project"]) or "All projects"
    df["project_label"] = df["project"].map(lambda p: shared if p == "ALL" else p)
    df["rag_n"] = df["project"].map(lambda p: _lookup(RAG, rag_by_proj.get(p, ""), worst if p == "ALL" else 0.2))

    load: dict[str, int] = {}
    for owners, is_open in zip(df["owners"], df["open"]):
        if is_open:
            for o in owners:
                if o.lower() not in IGNORE_FOR_LOAD and is_person(o):
                    load[o] = load.get(o, 0) + 1
    top = max(load.values(), default=1)
    df["contention"] = df["owners"].map(
        lambda os_: max([load.get(o, 0) for o in os_], default=0) / top if top > 1 else 0.0)
    df["urgency"] = df["due_first"].map(lambda d: 0.2 if d is None else min(1.0, max(0.1, 1 - d / 7)))

    f = {"Impact": df["impact_n"] / 4, "Probability": df["prob_n"], "Urgency (due date)": df["urgency"],
         "Release gate": df["gate"].astype(float), "Project health (RAG)": df["rag_n"],
         "Resource contention": df["contention"]}
    df["score"] = (100 * sum(w[c] * f[c] for c in CRITERIA)).round(1)
    df.loc[~df["open"], "score"] = 0.0

    leave = find_leave((df["description"] + " " + df["action"]).tolist())
    df["leave_conflict"] = [_leave_conflict(o, a, b, leave) for o, a, b in
                            zip(df["owners"], df["due_first"], df["due_last"])]
    df["flags"] = df.apply(_flags, axis=1)
    df["_d"] = df["due_first"].map(lambda d: 99 if d is None else d)
    df = df.sort_values(["score", "_d"], ascending=[False, True]).drop(columns="_d").reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    return df


def _leave_conflict(owners, first, last, leave) -> str:
    for o in owners:
        for name, (d1, d2) in leave.items():
            if o.split()[0].lower() == name.lower():
                for d in (first, last):
                    if d is not None and d1 <= d <= d2:
                        return f"{name} on leave {day_label(d1)}–{day_label(d2)} but item is due {day_label(d)}"
    return ""


def _flags(r) -> list[str]:
    fl = []
    if r["gate"]:
        fl.append("Release gate")
    if r["leave_conflict"]:
        fl.append("Owner on leave")
    if str(r["probability"]).lower() == "observed":
        fl.append("Already happened")
    if r["due_first"] is not None and r["due_first"] <= 0:
        fl.append("Due immediately")
    return fl


def enrich_projects(projects: pd.DataFrame, items: pd.DataFrame, anchor: date | None = None) -> pd.DataFrame:
    anchor = anchor or default_anchor()
    p = projects.copy()
    rows = []
    for _, r in p.iterrows():
        mine = items[(items["project"].isin([r["project"], "ALL"])) & items["open"]] if not items.empty else items
        tgt = parse_due(r["target"], anchor)
        rows.append({"open_items": len(mine), "critical_items": int((mine["impact_n"] >= 4).sum()) if len(mine) else 0,
                     "gates": int(mine["gate"].sum()) if len(mine) else 0,
                     "max_score": float(mine["score"].max()) if len(mine) else 0.0,
                     "target_offset": tgt["first"]})
    return pd.concat([p.reset_index(drop=True), pd.DataFrame(rows)], axis=1)


def kpis(items: pd.DataFrame, projects: pd.DataFrame) -> dict:
    op = items[items["open"]] if not items.empty else items
    return {"projects": len(projects),
            "red_amber": int(projects["rag"].str.lower().isin(["red", "amber"]).sum()),
            "open_items": len(op),
            "critical": int((op["impact_n"] >= 4).sum()) if len(op) else 0,
            "observed": int(op["probability"].str.lower().eq("observed").sum()) if len(op) else 0,
            "gates": int(op["gate"].sum()) if len(op) else 0,
            "leave_conflicts": int(op["leave_conflict"].ne("").sum()) if len(op) else 0}


def owner_day_matrix(items: pd.DataFrame, days: int = 5) -> pd.DataFrame:
    """Rows = people, columns = weekdays, values = open items first-due that day."""
    rows: dict[str, list[int]] = {}
    for owners, d, is_open in zip(items["owners"], items["due_first"], items["open"]):
        if not is_open or d is None or not 0 <= d < days:
            continue
        for o in owners:
            if is_person(o) and o.lower() not in IGNORE_FOR_LOAD:
                rows.setdefault(o, [0] * days)[d] += 1
    return pd.DataFrame.from_dict(rows, orient="index", columns=DAY_NAMES[:days]).sort_index()


def overloaded(matrix: pd.DataFrame, threshold: int = 2) -> list[str]:
    out = []
    for o, r in matrix.iterrows():
        for d, n in r.items():
            if n >= threshold:
                out.append(f"{o} has {n} items first due on {d}")
    return out


def slip_effect(items: pd.DataFrame, projects: pd.DataFrame, item_id: str, slip_days: int,
                anchor: date | None = None) -> dict:
    """What happens if item `item_id` slips by `slip_days`?"""
    row = items[items["id"] == item_id]
    if row.empty:
        raise KeyError(item_id)
    r = row.iloc[0]
    base = r["due_last"] if r["due_last"] is not None else r["due_first"]
    if base is None:
        return {"new_due": None, "breaches": [], "downstream": [], "note": "Item has no parsable due date."}
    new = int(base) + int(slip_days)
    scope = projects if r["project"] == "ALL" else projects[projects["project"] == r["project"]]
    breaches = []
    for _, p in scope.iterrows():
        t = parse_due(p["target"], anchor or default_anchor())["first"]
        if t is not None and new > t:
            breaches.append(f"{p['project']} target ({day_label(t)}) missed by {new - t} day(s)")
    same = items[(items["project"].isin([r["project"], "ALL"])) & items["open"] & (items["id"] != item_id)]
    downstream = same[(same["due_first"].notna()) & (same["due_first"] >= base)]["id"].tolist()
    return {"new_due": new, "new_due_label": day_label(new), "breaches": breaches, "downstream": downstream, "note": ""}


def coerce_order(items: list, out) -> list[str]:
    """Make a drag-and-drop component's return value safe to store and feed back in.

    Accepts a flat list of strings, or the container form [{"header":..., "items":[...]}] that some
    versions of streamlit-sortables return. Anything unusable (None, wrong types, items that no longer
    match the current list e.g. a stale widget state) falls back to the original order.
    """
    base = [str(i) for i in items]
    if isinstance(out, list) and out and all(isinstance(o, dict) for o in out):
        out = [x for o in out for x in (o.get("items") or [])]
    if isinstance(out, list) and all(isinstance(o, str) for o in out) and sorted(out) == sorted(base):
        return list(out)
    return base
