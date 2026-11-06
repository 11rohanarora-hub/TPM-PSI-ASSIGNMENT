"""Weekly status report per project -> Markdown / DOCX."""
from __future__ import annotations

import io
from datetime import date

import pandas as pd

from .analytics import day_label

SECTIONS = ["Executive summary", "Key risks & issues", "Decisions & controls", "Next steps this week",
            "Dependencies & asks", "Meetings & actions", "Completed this week"]
CLIENT_HIDDEN = {"Decisions & controls"}  # internal-only wording


def _bullets_from_text(t: str) -> list[str]:
    return [s.strip().rstrip(".") + "." for s in t.replace("•", ". ").split(". ") if s.strip()] if t else []


def _due_str(r) -> str:
    t = f" {r['due_time']}" if r.get("due_time") else ""
    return f"{day_label(r['due_first'])}{t}" if r["due_first"] is not None else "TBD"


def build_report(project: pd.Series, items: pd.DataFrame, meetings: pd.DataFrame | None = None,
                 actions: pd.DataFrame | None = None, anchor: date | None = None,
                 order: list[str] | None = None, audience: str = "internal",
                 narrative: str = "") -> dict:
    name = project["project"]
    mine = items[items["project"].isin([name, "ALL"]) & items["open"]].sort_values("rank") if len(items) else items
    sec: dict[str, list[str]] = {}

    s = [f"**Overall status: {str(project['rag']).upper()}**"]
    if project.get("problem"):
        s.append(f"Main issue: {project['problem']}")
    if project.get("milestone"):
        s.append(f"Next milestone: {project['milestone']} (target: {project.get('target') or 'TBD'})")
    s.append(f"{len(mine)} open item(s); {int(mine['gate'].sum()) if len(mine) else 0} release gate(s); "
             f"{int((mine['impact_n'] >= 4).sum()) if len(mine) else 0} critical.")
    if narrative:
        s.append(narrative)
    sec["Executive summary"] = s

    sec["Key risks & issues"] = [
        f"[{r['id']}] {r['description']} — {r['probability']}/{r['impact']}; owner: {r['owner']}; due: {_due_str(r)}"
        + (f" ⚠ {r['leave_conflict']}" if r["leave_conflict"] and audience == "internal" else "")
        for _, r in mine.head(6).iterrows()]
    sec["Decisions & controls"] = _bullets_from_text(project.get("decision", "")) + [
        f"Gate: [{r['id']}] {r['action'].split('. ')[0]}" for _, r in mine[mine["gate"]].iterrows()] if len(mine) else []
    steps = mine.assign(_d=mine["due_first"].map(lambda d: 99 if d is None else d)).sort_values(["_d", "rank"])
    sec["Next steps this week"] = [f"{_due_str(r)} — {r['action'] or r['description']} ({r['owner']})"
                                   for _, r in steps.iterrows()]
    deps = mine[mine["type"].str.lower().str.contains("depend|scope") | mine["status"].str.lower().str.contains("external|waiting")] \
        if len(mine) else mine
    sec["Dependencies & asks"] = [f"[{r['id']}] {r['description']} — next: {r['action'].split('. ')[0]}" for _, r in deps.iterrows()]

    ml, done = [], []
    if meetings is not None and len(meetings):
        for _, m in meetings[meetings["Project"] == name].tail(3).iterrows():
            ml.append(f"{m['Date']} — {m['Title']}" + (f": {m['Decisions']}" if m["Decisions"] else ""))
    if actions is not None and len(actions):
        a = actions[actions["Project"] == name]
        ml += [f"Open action: {r['Action']} ({r['Owner']}, due {r['Due'] or 'TBD'})" for _, r in a[~a["Done"].astype(bool)].iterrows()]
        done = [f"{r['Action']} ({r['Owner']})" for _, r in a[a["Done"].astype(bool)].iterrows()]
    sec["Meetings & actions"] = ml
    sec["Completed this week"] = done or ["Nothing marked complete yet. Tick actions as done in the Meetings tab "
                                          "(the tracker itself has no 'completed' column)."]

    order = [o for o in (order or SECTIONS) if o in SECTIONS]
    if audience == "client":
        order = [o for o in order if o not in CLIENT_HIDDEN]
    week = f"week of {anchor:%d %b %Y}" if anchor else "this week"
    return {"title": f"Weekly Status — {name}", "subtitle": f"{week} • prepared by TPM",
            "sections": [(o, sec[o]) for o in order if sec.get(o)]}


def to_markdown(rep: dict) -> str:
    out = [f"# {rep['title']}", f"_{rep['subtitle']}_", ""]
    for name, lines in rep["sections"]:
        out.append(f"## {name}")
        out += [f"- {l}" for l in lines]
        out.append("")
    return "\n".join(out).strip() + "\n"


def to_docx(reports: list[dict]) -> bytes:
    from docx import Document
    doc = Document()
    for i, rep in enumerate(reports):
        if i:
            doc.add_page_break()
        doc.add_heading(rep["title"], 0)
        doc.add_paragraph(rep["subtitle"]).italic = True
        for name, lines in rep["sections"]:
            doc.add_heading(name, 2)
            for l in lines:
                p = doc.add_paragraph(style="List Bullet")
                for j, part in enumerate(l.split("**")):  # **bold** markers
                    run = p.add_run(part)
                    run.bold = j % 2 == 1
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
