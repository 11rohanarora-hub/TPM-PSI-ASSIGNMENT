"""Grounded Q&A + recommendations. LLM when available, deterministic rules otherwise."""
from __future__ import annotations

import re

import pandas as pd

from .analytics import DAY_NAMES, _DAY_ALIASES, day_label, is_person, owner_day_matrix, overloaded
from .llm import LLM, LLMError

SYSTEM_CHAT = (
    "You are a TPM assistant answering questions about ONE project tracker. Use ONLY the tracker "
    "data provided. Cite item IDs in brackets like [KAV-02]. If the answer is not in the data, say "
    "'That is not in the tracker.' Never invent dates, owners or numbers. Be concise.")
SYSTEM_ADVISOR = (
    "You are a senior Technical Program Manager coach. Using ONLY the tracker data and the computed "
    "analysis provided, recommend what to do first and how to proceed effectively. Output: "
    "1) a ranked 'Do first' list (max 5) with the reason and the expected effect on the timeline; "
    "2) 'Risks to the timeline' (leave, capacity, dependencies); 3) 'Decisions to escalate'. "
    "Cite item IDs. Do not invent facts. Keep it under 350 words.")


def _row_line(r) -> str:
    return (f"[{r['id']}] ({r.get('project_label', r['project'])}) {r['type']} | status {r['status']} | {r['probability']}/{r['impact']} | "
            f"owner {r['owner']} | due {r['due']} | {r['description']} | next: {r['action']}")


def build_context(projects: pd.DataFrame, items: pd.DataFrame, question: str | None = None,
                  k: int = 30, extras: str = "") -> str:
    """Compact grounding text. For big trackers keep only the k rows most relevant to the question."""
    lines = ["PROJECTS:"]
    for _, p in projects.iterrows():
        lines.append(f"- {p['project']} | RAG {p['rag']} | milestone: {p['milestone']} | target: {p['target']} | "
                     f"problem: {p['problem']} | decision: {p['decision']}")
    rows = items
    if question and len(items) > k:
        rows = retrieve(items, question, k)
    lines.append("ITEMS (ranked by computed priority):" if "rank" in rows else "ITEMS:")
    lines += [_row_line(r) for _, r in rows.iterrows()]
    if extras:
        lines += ["", extras]
    return "\n".join(lines)


STOP = {"the", "and", "for", "what", "whats", "who", "how", "when", "where", "which", "why", "are", "was", "does",
        "did", "has", "have", "with", "this", "that", "from", "about", "any", "all", "can", "you", "tell", "show",
        "give", "list", "there", "their", "its", "our", "not", "but", "will", "should", "would", "could", "item",
        "items", "many", "much", "please"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) > 2 and t not in STOP}


def retrieve(items: pd.DataFrame, question: str, k: int = 5) -> pd.DataFrame:
    q = _tokens(question)
    blob = items.apply(lambda r: _tokens(" ".join(str(v) for v in r.values if isinstance(v, str))), axis=1)
    score = blob.map(lambda b: len(q & b))
    return items.assign(_s=score).sort_values("_s", ascending=False, kind="stable").head(k).drop(columns="_s")


def fmt_item(r) -> str:
    sc = f" · priority {r['score']:.0f}" if "score" in r and pd.notna(r["score"]) else ""
    return (f"**{r['id']}** ({r.get('project_label', r['project'])}) — {r['description']}  \n"
            f"  {r['probability']} / {r['impact']} · Owner: {r['owner']} · Due: {r['due']}{sc}")


def offline_answer(question: str, items: pd.DataFrame, projects: pd.DataFrame) -> str:
    """Rule-based answers for the common TPM questions - works with no model at all."""
    q = question.lower()
    if items.empty:
        return "The tracker has no items."
    ids = {m.upper() for m in re.findall(r"\b([a-z]{1,5}-\d+)\b", q)}
    if ids:
        hit = items[items["id"].str.upper().isin(ids)]
        if not hit.empty:
            return "\n\n".join(fmt_item(r) + f"\n  Next: {r['action']}" for _, r in hit.iterrows())
        return f"No item with ID {', '.join(sorted(ids))} in the tracker."

    sel = items
    filters = []
    owners = sorted({o for os_ in items["owners"] for o in os_ if is_person(o)}, key=len, reverse=True) \
        if "owners" in items else []
    for o in owners:
        if re.search(rf"\b{re.escape(o.lower())}\b", q):
            sel = sel[sel["owners"].map(lambda os_, o=o: any(x.lower().startswith(o.lower().split()[0]) for x in os_))]
            filters.append(f"owner {o}")
            break
    for w in re.findall(r"[a-z]+", q):
        if w in _DAY_ALIASES and _DAY_ALIASES[w] < 7:
            d = _DAY_ALIASES[w]
            sel = sel[sel["due_first"].map(lambda x, d=d: x == d or False)] if "due_first" in sel else sel
            filters.append(f"due {DAY_NAMES[d]}")
            break
    for _, p in projects.iterrows():
        first = re.split(r"\s[–—-]\s", p["project"])[0].lower()
        if first in q or p["project"].lower() in q:
            sel = sel[sel["project"].isin([p["project"], "ALL"])]
            filters.append(p["project"])
            break
    if filters:
        if sel.empty:
            return f"Nothing in the tracker matches: {', '.join(filters)}."
        if re.search(r"how many|count|number of|total", q):
            return f"{len(sel)} item(s) — {', '.join(filters)}: " + ", ".join(sel["id"])
        return f"{len(sel)} item(s) — {', '.join(filters)}:\n\n" + "\n\n".join(fmt_item(r) for _, r in sel.iterrows())

    ranked = items.sort_values("rank") if "rank" in items else items
    if re.search(r"first|priorit|urgent|top|focus|important", q):
        return "Do these first (highest computed priority):\n\n" + "\n\n".join(fmt_item(r) for _, r in ranked.head(3).iterrows())
    if re.search(r"gate|block|go/no-go|launch|pilot", q):
        g = ranked[ranked["gate"]] if "gate" in ranked else ranked.iloc[0:0]
        return ("Release gates / blockers:\n\n" + "\n\n".join(fmt_item(r) for _, r in g.iterrows())) if len(g) \
            else "No release-gate items detected."
    if re.search(r"leave|vacation|conflict|capacity|overload|availab", q):
        c = ranked[ranked["leave_conflict"].ne("")] if "leave_conflict" in ranked else ranked.iloc[0:0]
        lines = [f"- **{r['id']}**: {r['leave_conflict']}" for _, r in c.iterrows()]
        lines += [f"- {x}" for x in overloaded(owner_day_matrix(items))] if "owners" in items else []
        return "Timeline / capacity problems:\n" + "\n".join(lines) if lines else "No leave or capacity conflicts detected."
    if re.search(r"how many|count|number of|total", q):
        op = int(items["open"].sum()) if "open" in items else len(items)
        by = items.groupby("project").size().to_dict()
        return f"{op} open item(s) of {len(items)} total. By project: " + ", ".join(f"{k}: {v}" for k, v in by.items())
    if re.search(r"status|rag|red|amber|green|health", q):
        return "\n".join(f"- **{p['project']}**: {p['rag']} — {p['problem']}" for _, p in projects.iterrows())
    top = retrieve(items, question, 3)
    if not (_tokens(question) & _tokens(" ".join(top["description"] + " " + top["action"]))):
        return "That is not in the tracker. Try asking about an item ID, an owner, a weekday, a project, gates, or priorities."
    return "Closest matches in the tracker:\n\n" + "\n\n".join(fmt_item(r) for _, r in top.iterrows())


def rule_recommendations(items: pd.DataFrame, projects: pd.DataFrame) -> list[dict]:
    """Deterministic 'what to do first' list with reason + expected effect."""
    recs = []
    op = items[items["open"]].sort_values("rank") if len(items) else items
    for _, r in op.head(5).iterrows():
        why = [f"{r['probability']} / {r['impact']}", f"due {r['due'] or 'TBD'}"]
        if r["gate"]:
            effect = "Clears a release gate - without it the pilot/launch cannot proceed."
        elif str(r["probability"]).lower() == "observed":
            effect = "Stops an issue that has already happened from growing."
        else:
            effect = "Reduces risk exposure before it becomes an issue."
        if r["leave_conflict"]:
            why.append(r["leave_conflict"])
            effect += " Needs a handover first."
        recs.append({"title": f"[{r['id']}] {r['action'] or r['description']}", "why": "; ".join(why),
                     "effect": effect, "owner": r["owner"]})
    for r in overloaded(owner_day_matrix(items)):
        recs.append({"title": "Rebalance workload", "why": r,
                     "effect": "Avoids a single person becoming the bottleneck on that day.", "owner": "TPM"})
    return recs


def recommendations_markdown(items, projects, llm: LLM | None = None, extras: str = "") -> tuple[str, str]:
    """Returns (markdown, source) where source is the model id or 'rule-based'."""
    rules = rule_recommendations(items, projects)
    rules_md = "\n".join(f"{i}. **{x['title']}** — {x['why']}. *Effect:* {x['effect']} (Owner: {x['owner']})"
                         for i, x in enumerate(rules, 1))
    if llm and llm.available:
        try:
            ctx = build_context(projects, items, extras=extras)
            out = llm.complete(SYSTEM_ADVISOR, f"{ctx}\n\nCOMPUTED PRIORITIES:\n{rules_md}", max_tokens=700)
            return out, llm.model
        except LLMError as exc:
            return rules_md + f"\n\n_(LLM unavailable: {exc}. Showing rule-based advice.)_", "rule-based"
    return rules_md, "rule-based"


def chat_answer(question: str, history: list[dict], items, projects, llm: LLM | None = None) -> tuple[str, str]:
    """Returns (answer, source)."""
    if llm and llm.available:
        try:
            ctx = build_context(projects, items, question=question)
            msgs = history[-6:]
            ans = llm.complete(SYSTEM_CHAT + "\n\nTRACKER DATA:\n" + ctx, question, history=msgs, max_tokens=500)
            return ans, llm.model
        except LLMError as exc:
            return offline_answer(question, items, projects) + f"\n\n_(LLM unavailable: {exc})_", "rule-based"
    return offline_answer(question, items, projects), "rule-based"


def exec_narrative(llm: LLM | None, project: pd.Series, items: pd.DataFrame) -> str:
    """2-3 sentence AI summary for the weekly report; '' when no LLM so the report stays factual."""
    if not (llm and llm.available):
        return ""
    mine = items[items["project"].isin([project["project"], "ALL"]) & items["open"]].sort_values("rank")
    ctx = build_context(pd.DataFrame([project]), mine)
    try:
        return llm.complete("Write a 2-3 sentence executive summary of this project's week for a client-facing "
                            "status report. Use only the data given; do not invent facts.", ctx, max_tokens=200)
    except LLMError:
        return ""
