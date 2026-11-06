"""Meeting tracker: meetings + action items, persisted to a local JSON file."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

MEETING_COLS = ["Date", "Project", "Title", "Attendees", "Agenda", "Notes", "Decisions"]
ACTION_COLS = ["Project", "Meeting", "Action", "Owner", "Due", "Done"]
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "meetings.json"


def empty_meetings() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=str) for c in MEETING_COLS})


def empty_actions() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype=str) for c in ACTION_COLS})
    df["Done"] = df["Done"].astype(bool)
    return df


def load(path: Path = DATA_FILE) -> tuple[pd.DataFrame, pd.DataFrame]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        m = pd.DataFrame(raw.get("meetings", []), columns=MEETING_COLS).fillna("").astype(str)
        a = pd.DataFrame(raw.get("actions", []), columns=ACTION_COLS)
        for c in ACTION_COLS[:-1]:
            a[c] = a[c].fillna("").astype(str)
        a["Done"] = a["Done"].fillna(False).astype(bool)
        return m, a
    except (OSError, ValueError, KeyError):  # missing or corrupt file -> start fresh, never crash
        return empty_meetings(), empty_actions()


def save(meetings: pd.DataFrame, actions: pd.DataFrame, path: Path = DATA_FILE) -> bool:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        payload = {"meetings": meetings.fillna("").astype(str).to_dict("records"),
                   "actions": actions.fillna("").to_dict("records")}
        Path(path).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return True
    except OSError:
        return False


def suggest_agenda(items: pd.DataFrame, project: str, limit: int = 5) -> list[str]:
    """Agenda for the next client call from the highest-priority open items."""
    if items.empty:
        return []
    mine = items[items["project"].isin([project, "ALL"]) & items["open"]].sort_values("rank").head(limit)
    agenda = [f"[{r['id']}] {r['description'].split('. ')[0].rstrip('.')} — decision/next step: "
              f"{(r['action'].split('. ')[0] or 'confirm owner and date').rstrip('.')}" for _, r in mine.iterrows()]
    return agenda + ["Confirm weekly reporting cadence and next meeting"]


def open_actions(actions: pd.DataFrame, project: str | None = None) -> pd.DataFrame:
    a = actions[~actions["Done"].astype(bool)] if len(actions) else actions
    return a[a["Project"] == project] if project and len(a) else a
