"""Draft client / owner / stakeholder messages and turn them into send-by reminders."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import pandas as pd

from .analytics import day_label, is_person
from .parser import client_of

ROLE_RE = re.compile(r"\b(Group CTO|CTO|COO|CEO|CFO|CIO|VP|Director|Sponsor)\b")
INTERNAL = {"tpm"}


@dataclass
class Draft:
    id: str
    kind: str            # client_update | owner_nudge | stakeholder
    project: str
    to_name: str
    to_email: str
    subject: str
    body: str
    send_day: int        # days from week anchor (Monday)
    send_time: str       # HH:MM
    item_ids: list[str] = field(default_factory=list)
    reason: str = ""


def _id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def find_contact(contacts: pd.DataFrame, name: str = "", role: str = "", project: str = "") -> str:
    if contacts is None or contacts.empty:
        return ""
    c = contacts.fillna("")
    for _, r in c.iterrows():
        if not r["Email"]:
            continue
        if name and r["Name"].strip().lower() == name.strip().lower():
            return r["Email"]
    for _, r in c.iterrows():
        if r["Email"] and role and role.lower() in r["Role"].lower() and \
                (not project or project.lower() in r["Project"].lower() or r["Project"].lower() in project.lower()):
            return r["Email"]
    return ""


def contact_template(items: pd.DataFrame, projects: pd.DataFrame) -> pd.DataFrame:
    """Pre-fill the address book with everyone/everything the tracker mentions (emails blank)."""
    rows = []
    for _, p in projects.iterrows():
        rows.append({"Name": f"{client_of(p['project'])} contact", "Email": "", "Role": "Client lead", "Project": p["project"]})
    people = sorted({o for os_ in items["owners"] for o in os_ if is_person(o) and o.lower() not in INTERNAL})
    rows += [{"Name": o, "Email": "", "Role": "Team", "Project": ""} for o in people]
    seen = set()
    for _, r in items.iterrows():
        for role in ROLE_RE.findall(f"{r['description']} {r['action']} {r['due']}"):
            if (role, r["project"]) not in seen:
                seen.add((role, r["project"]))
                rows.append({"Name": role, "Email": "", "Role": role, "Project": r["project"]})
    return pd.DataFrame(rows, columns=["Name", "Email", "Role", "Project"])


def _t(r) -> str:
    return f" {r['due_time']}" if r["due_time"] else ""


def build_drafts(items: pd.DataFrame, projects: pd.DataFrame, contacts: pd.DataFrame | None,
                 anchor: date, sender: str = "Your TPM", audience_note: bool = True) -> list[Draft]:
    drafts: list[Draft] = []
    live = items[items["open"]]
    # 1) weekly client update per project
    for _, p in projects.iterrows():
        mine = live[live["project"].isin([p["project"], "ALL"])].sort_values("rank")
        if mine.empty:
            continue
        client = client_of(p["project"])
        email = find_contact(contacts, name=f"{client} contact") or find_contact(contacts, role="Client lead", project=p["project"])
        urgent = mine[mine["description"].str.contains(r"status report .*not sent|missed", case=False, regex=True)]
        day, tm = (int(urgent.iloc[0]["due_first"]), urgent.iloc[0]["due_time"] or "13:00") \
            if len(urgent) and urgent.iloc[0]["due_first"] is not None else (4, "16:00")
        key = "\n".join(f"- {r['description'].split('. ')[0].rstrip('.')} (next: {(r['action'].split('. ')[0] or 'TBD').rstrip('.')})"
                        for _, r in mine.head(4).iterrows())
        steps = "\n".join(f"- {day_label(r['due_first'])}{_t(r)}: {(r['action'].split('. ')[0] or r['description']).rstrip('.')}"
                          for _, r in mine.sort_values("due_first", key=lambda s: s.map(lambda d: 99 if d is None else d)).head(5).iterrows())
        asks = mine[mine["owner"].str.contains(client.split()[0], case=False)]
        ask_txt = "\n\nWhere we need your help:\n" + "\n".join(
            f"- [{r['id']}] {r['action'].split('. ')[0].rstrip('.')}" for _, r in asks.iterrows()) if len(asks) else ""
        body = (f"Hi {client} team,\n\nHere is the weekly status for {p['project'].split(' – ')[0]} "
                f"(week of {anchor:%d %b %Y}).\n\nOverall status: {str(p['rag']).upper()}\n\nKey points:\n{key}\n\n"
                f"Next steps this week:\n{steps}{ask_txt}\n\nPlease reply with any questions; I will confirm progress "
                f"at the next checkpoint.\n\nRegards,\n{sender}")
        drafts.append(Draft(_id("client", p["project"]), "client_update", p["project"], f"{client} contact", email,
                            f"Weekly status – {p['project']} ({str(p['rag']).upper()})", body, day, tm,
                            mine["id"].tolist(), "Weekly client status"))
    # 2) one nudge per internal owner, sent the afternoon before their first due item
    per_owner: dict[str, list] = {}
    for _, r in live.iterrows():
        for o in r["owners"]:
            if is_person(o) and o.lower() not in INTERNAL:
                per_owner.setdefault(o, []).append(r)
    for o, rows in per_owner.items():
        rows = sorted(rows, key=lambda r: 99 if r["due_first"] is None else r["due_first"])
        first = rows[0]["due_first"]
        day, tm = (0, "09:00") if first is None or first <= 0 else (first - 1, "16:00")
        lines = []
        for r in rows:
            warn = f"  ⚠ {r['leave_conflict']}" if r["leave_conflict"] and o.split()[0] in r["leave_conflict"] else ""
            lines.append(f"- [{r['id']}] {day_label(r['due_first'])}{_t(r)}: {(r['action'] or r['description']).split('. ')[0].rstrip('.')}{warn}")
        body = (f"Hi {o.split()[0]},\n\nQuick reminder of what is due from you this week:\n\n" + "\n".join(lines) +
                "\n\nPlease reply with a status or a blocker by end of day. If any date is at risk, tell me now so we can "
                f"re-plan.\n\nThanks,\n{sender}")
        drafts.append(Draft(_id("owner", o), "owner_nudge", ", ".join(sorted({r["project"] for r in rows})), o,
                            find_contact(contacts, name=o), f"Reminder: {len(rows)} item(s) due this week", body, day, tm,
                            [r["id"] for r in rows], "Owner reminder"))
    # 3) stakeholder escalations when an item names a role (CTO, COO, ...)
    seen = set()
    for _, r in live.sort_values("rank").iterrows():
        m = ROLE_RE.search(f"{r['description']} {r['action']} {r['due']}")
        if not m or (m.group(1), r["project"]) in seen:
            continue
        seen.add((m.group(1), r["project"]))
        day = r["due_first"] if r["due_first"] is not None else 4
        body = (f"Hi,\n\nUpdate on [{r['id']}] ({r.get('project_label', r['project'])}):\n\n{r['description']}\n\nNext action: {r['action']}\n\n"
                f"Owner: {r['owner']} · Due: {r['due']}\n\nHappy to walk through details on a call.\n\nRegards,\n{sender}")
        drafts.append(Draft(_id("role", m.group(1), r["project"]), "stakeholder", r["project"], m.group(1),
                            find_contact(contacts, role=m.group(1), project=r["project"]) or find_contact(contacts, name=m.group(1)),
                            f"Update: {r['id']} – {r['description'].split('. ')[0].rstrip('.')}", body, int(day),
                            r["due_time"] or "10:00", [r["id"]], f"{m.group(1)} named in {r['id']}"))
    return sorted(drafts, key=lambda d: (d.send_day, d.send_time))


def reminder_state(d: Draft, now_day: int, now_time: str) -> str:
    if (d.send_day, d.send_time) < (now_day, now_time):
        return "overdue"
    return "due today" if d.send_day == now_day else "upcoming"


def mailto(d: Draft) -> str:
    return f"mailto:{quote(d.to_email)}?subject={quote(d.subject, safe='')}&body={quote(d.body, safe='')}"


def _ics_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(drafts: list[Draft], anchor: date, minutes_before: int = 15) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TPM Copilot//EN", "CALSCALE:GREGORIAN"]
    for d in drafts:
        h, m = (int(x) for x in d.send_time.split(":"))
        start = datetime.combine(anchor + timedelta(days=d.send_day), datetime.min.time()).replace(hour=h, minute=m)
        end = start + timedelta(minutes=15)
        lines += ["BEGIN:VEVENT", f"UID:{d.id}@tpm-copilot", f"DTSTAMP:{stamp}",
                  f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{end:%Y%m%dT%H%M%S}",
                  f"SUMMARY:{_ics_escape('Send: ' + d.subject)}",
                  f"DESCRIPTION:{_ics_escape(f'To: {d.to_name} {d.to_email}' + chr(10) + d.reason)}",
                  "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape('Send: ' + d.subject)}",
                  f"TRIGGER:-PT{minutes_before}M", "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
