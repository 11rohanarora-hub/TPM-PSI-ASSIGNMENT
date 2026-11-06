"""TPM Copilot - upload a tracker, get analytics, AI advice, chat, weekly status, meetings & reminders."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

from tpm_tool import advisor, charts, meetings as mt, messages as msg, report as rp
from tpm_tool.analytics import (CRITERIA, coerce_order, DAY_NAMES, DEFAULT_ORDER, day_label, default_anchor, enrich,
                                enrich_projects, kpis, monday_of, overloaded, owner_day_matrix, slip_effect,
                                weights_from_order)
from tpm_tool.llm import DEFAULT_MODEL, LLM
from tpm_tool.parser import ParseError, parse_tracker

try:
    from streamlit_sortables import sort_items
except ImportError:  # app still works, just without drag & drop
    sort_items = None

ROOT = Path(__file__).parent
SAMPLE = ROOT / "sample_data" / "project_tracker.xlsx"
CONTACTS_FILE = ROOT / "data" / "contacts.json"
RAG_ICON = {"red": "🔴", "amber": "🟠", "green": "🟢"}

st.set_page_config(page_title="TPM Copilot", page_icon="🧭", layout="wide")


# ---------- small helpers ----------
def stretch(fn, *a, **kw):
    """Call a Streamlit widget full-width across versions (use_container_width was renamed)."""
    try:
        return fn(*a, use_container_width=True, **kw)
    except TypeError:
        return fn(*a, width="stretch", **kw)


def show_fig(fig, key: str):
    if fig is None:
        st.caption("Nothing to chart for the current data.")
    else:
        stretch(st.plotly_chart, fig, key=key)


def drag_order(items: list[str], key: str) -> list[str]:
    """Drag-and-drop reorder list; falls back to the given order if the component is missing or misbehaves."""
    items = [str(i) for i in items]  # the component rejects anything but a flat list of strings
    if sort_items is None:
        st.caption("Install `streamlit-sortables` to enable drag & drop (order shown is the default).")
        return items
    try:
        out = sort_items(items, direction="vertical", key=key)
    except Exception as e:  # never let a UI widget take the whole app down
        st.caption(f"Drag & drop unavailable ({e}); using default order.")
        return items
    return coerce_order(items, out)


@st.cache_data(show_spinner="Reading tracker…")
def cached_parse(data: bytes, name: str):
    return parse_tracker(data, name)


def load_contacts() -> pd.DataFrame:
    try:
        return pd.DataFrame(json.loads(CONTACTS_FILE.read_text()), columns=["Name", "Email", "Role", "Project"]).fillna("")
    except (OSError, ValueError):
        return pd.DataFrame(columns=["Name", "Email", "Role", "Project"])


def save_contacts(df: pd.DataFrame):
    try:
        CONTACTS_FILE.parent.mkdir(exist_ok=True)
        CONTACTS_FILE.write_text(json.dumps(df.fillna("").to_dict("records"), indent=2))
    except OSError:
        st.toast("Could not save contacts to disk (read-only folder?).")


def polish(model: LLM, key: str):
    """Button callback (runs before the rerun, so it may safely overwrite the text_area's state)."""
    try:
        ss[key] = model.complete("Rewrite this message to be concise, polite and professional. Keep every fact, "
                                 "ID and date unchanged. Return only the message body.", ss[key])
        ss.pop("polish_err", None)
    except Exception as e:  # LLMError or anything else: never lose the draft
        ss["polish_err"] = str(e)


# ---------- sidebar ----------
ss = st.session_state
ss.setdefault("order", list(DEFAULT_ORDER))
ss.setdefault("chat", [])
ss.setdefault("sent", set())
ss.setdefault("recs", None)

with st.sidebar:
    st.title("🧭 TPM Copilot")
    up = st.file_uploader("Drag & drop your tracker (.xlsx / .csv)", type=["xlsx", "xls", "csv", "tsv"])
    use_sample = st.checkbox("Use bundled sample tracker", value=up is None, disabled=up is not None)
    anchor = monday_of(st.date_input("Week starting (Monday)", value=default_anchor()))
    today_idx = date.today().weekday() if date.today().weekday() < 5 else 0
    now_day = DAY_NAMES.index(st.selectbox("Simulate 'today' as", DAY_NAMES[:5], index=today_idx))
    sender = st.text_input("Your name (for signatures)", "Your TPM")

    with st.expander("🤖 AI model (Hugging Face)", expanded=False):
        token = st.text_input("HF token", type="password", help="Or set HF_TOKEN. Leave empty for rule-based mode.")
        model = st.text_input("Model repo id", DEFAULT_MODEL)
        base = st.text_input("Custom endpoint (optional)", placeholder="http://localhost:8000/v1")
    llm = LLM(token=token or None, model=model, base_url=base or None)
    st.caption(f"AI: **{llm.model}**" if llm.available else "AI: **rule-based mode** (no token)")

    with st.expander("↕️ Priority preferences — drag to reorder", expanded=True):
        st.caption("Top = most important when ranking what to do first.")
        ss["order"] = drag_order(ss["order"], "order_sort")

# ---------- load + analyse ----------
if up is not None:
    raw, fname = up.getvalue(), up.name
elif use_sample and SAMPLE.exists():
    raw, fname = SAMPLE.read_bytes(), SAMPLE.name
else:
    st.info("⬅️ Upload a tracker (.xlsx or .csv) to begin, or tick the bundled sample.")
    st.stop()
try:
    parsed = cached_parse(raw, fname)
except ParseError as e:
    st.error(str(e))
    st.stop()

for w in parsed.warnings:
    st.warning(w)
weights = weights_from_order(ss["order"])
items = enrich(parsed.items, parsed.projects, weights, anchor)
projects = enrich_projects(parsed.projects, items, anchor)

if "meetings" not in ss:
    ss["meetings"], ss["actions"] = mt.load()
if "contacts" not in ss:
    base_c = load_contacts()
    tmpl = msg.contact_template(items, projects)
    ss["contacts"] = pd.concat([base_c, tmpl[~tmpl["Name"].isin(base_c["Name"])]], ignore_index=True)

st.caption(f"📄 {fname} • sheets read: " + ", ".join(f"{k} → {v}" for k, v in parsed.sheets_used.items()))
tabs = st.tabs(["📊 Dashboard", "🎯 Priorities & timeline", "💡 AI advisor", "💬 Chat with tracker",
                "📝 Weekly status", "🗓️ Meetings", "✉️ Messages & reminders", "🧾 Data"])

# ---------- 1 dashboard ----------
with tabs[0]:
    k = kpis(items, projects)
    cols = st.columns(6)
    for c, (label, val) in zip(cols, [("Projects", k["projects"]), ("Red / Amber", k["red_amber"]),
                                      ("Open items", k["open_items"]), ("Critical", k["critical"]),
                                      ("Already happened", k["observed"]), ("Release gates", k["gates"])]):
        c.metric(label, val)
    if k["leave_conflicts"]:
        st.error(f"⚠️ {k['leave_conflicts']} item(s) fall due while their owner is on leave — see Priorities tab.")
    pc = st.columns(max(1, len(projects)))
    for c, (_, p) in zip(pc, projects.iterrows()):
        with c:
            st.subheader(f"{RAG_ICON.get(str(p['rag']).lower(), '⚪')} {p['project']}")
            st.write(f"**Next milestone:** {p['milestone'] or '—'}")
            st.write(f"**Target:** {p['target'] or '—'}")
            st.write(f"**Main problem:** {p['problem'] or '—'}")
    a, b = st.columns(2)
    with a:
        show_fig(charts.prob_impact_heatmap(items), "heat")
        show_fig(charts.project_load(projects), "pload")
    with b:
        show_fig(charts.priority_bar(items), "pbar")
        show_fig(charts.type_mix(items), "types")

# ---------- 2 priorities & timeline ----------
with tabs[1]:
    st.subheader("What should be done first")
    st.caption("Ranking = weighted blend of " + ", ".join(f"{c} ({weights[c]:.0%})" for c in ss["order"])
               + ". Drag criteria in the sidebar to change it.")
    view = items.assign(Flags=items["flags"].map(", ".join), Due=items["due"], Project=items["project_label"]).loc[
        :, ["rank", "id", "Project", "score", "impact", "probability", "owner", "Due", "Flags", "action"]]
    stretch(st.dataframe, view, hide_index=True, column_config={
        "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%.0f")})
    st.subheader("Effect of timelines")
    show_fig(charts.timeline(items, projects, anchor), "timeline")
    l, r = st.columns(2)
    with l:
        show_fig(charts.owner_heatmap(items), "ownerheat")
    with r:
        st.markdown("**Timeline & capacity problems**")
        probs = [f"**{x['id']}** — {x['leave_conflict']}" for _, x in items[items["leave_conflict"] != ""].iterrows()]
        probs += overloaded(owner_day_matrix(items))
        st.write("\n".join(f"- {p}" for p in probs) if probs else "None detected.")
    st.markdown("**Slip simulator — what if an item slips?**")
    s1, s2 = st.columns(2)
    desc = dict(zip(items["id"], items["description"]))
    pick = s1.selectbox("Item", list(desc), format_func=lambda i: f"{i} — {desc[i][:60]}")
    days = s2.slider("Slips by (days)", 0, 5, 1)
    res = slip_effect(items, projects, pick, days, anchor)
    if res["note"]:
        st.info(res["note"])
    else:
        st.write(f"New date: **{res['new_due_label']}**")
        for bmsg in res["breaches"]:
            st.error("❌ " + bmsg)
        if not res["breaches"]:
            st.success("No project target is breached.")
        if res["downstream"]:
            st.warning("Compressed/at-risk follow-ons: " + ", ".join(res["downstream"]))

# ---------- 3 advisor ----------
with tabs[2]:
    st.subheader("Recommended plan")
    if st.button("Generate recommendations", type="primary"):
        with st.spinner("Thinking…"):
            extras = "TIMELINE PROBLEMS:\n" + "\n".join(
                [x["leave_conflict"] for _, x in items.iterrows() if x["leave_conflict"]] + overloaded(owner_day_matrix(items)))
            ss["recs"] = advisor.recommendations_markdown(items, projects, llm, extras)
    if ss["recs"]:
        md, src = ss["recs"]
        st.markdown(md)
        st.caption(f"Source: {src}")
    else:
        st.markdown("\n".join(f"{i}. **{x['title']}** — {x['why']}. *Effect:* {x['effect']}"
                              for i, x in enumerate(advisor.rule_recommendations(items, projects)[:5], 1)))
        st.caption("Rule-based preview. Click the button for the LLM version (needs an HF token).")

# ---------- 4 chat ----------
with tabs[3]:
    st.subheader("Ask your tracker")
    qs = ["What should I do first?", "Who is on leave and what does it affect?", "What are the release gates?",
          "What is due on Tuesday?", "Status of each project"]
    qc = st.columns(len(qs))
    clicked = next((q for c, q in zip(qc, qs) if c.button(q, key=f"q_{q}")), None)
    if st.button("Clear chat"):
        ss["chat"] = []
    for m in ss["chat"]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])
            if m.get("src"):
                st.caption(f"answered by {m['src']}")
    q = st.chat_input("e.g. What is KAV-02? / Which items does Arjun own? / How many risks does Ledger have?") or clicked
    if q:
        ss["chat"].append({"role": "user", "content": q})
        hist = [{"role": m["role"], "content": m["content"]} for m in ss["chat"][:-1]]
        ans, src = advisor.chat_answer(q, hist, items, projects, llm)
        ss["chat"].append({"role": "assistant", "content": ans, "src": src})
        st.rerun()

# ---------- 5 weekly status ----------
with tabs[4]:
    st.subheader("Weekly status report")
    c1, c2 = st.columns([2, 1])
    with c1:
        who = st.multiselect("Projects", projects["project"].tolist(), default=projects["project"].tolist())
        audience = st.radio("Audience", ["internal", "client"], horizontal=True,
                            help="Client version hides internal controls and owner-leave warnings.")
        ai_sum = st.checkbox("Add AI executive summary", value=False, disabled=not llm.available)
    with c2:
        st.caption("Drag to reorder sections")
        sec_order = drag_order(list(rp.SECTIONS), "sections")
    reports, narr = [], ""
    for _, p in projects[projects["project"].isin(who)].iterrows():
        if ai_sum:
            narr = advisor.exec_narrative(llm, p, items)
        reports.append(rp.build_report(p, items, ss["meetings"], ss["actions"], anchor, sec_order, audience, narr))
    for rep in reports:
        with st.expander(rep["title"], expanded=True):
            st.markdown(rp.to_markdown(rep))
    if reports:
        d1, d2, d3 = st.columns(3)
        combined = "\n\n---\n\n".join(rp.to_markdown(r) for r in reports)
        d1.download_button("⬇️ Markdown", combined, f"weekly_status_{anchor}.md", "text/markdown")
        d2.download_button("⬇️ Word (.docx)", rp.to_docx(reports), f"weekly_status_{anchor}.docx",
                           "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        zb = io.BytesIO()
        with zipfile.ZipFile(zb, "w") as z:
            for r in reports:
                z.writestr(r["title"].replace(" ", "_").replace("–", "-") + ".md", rp.to_markdown(r))
        d3.download_button("⬇️ One file per client (.zip)", zb.getvalue(), f"weekly_status_{anchor}.zip", "application/zip")
    else:
        st.info("Select at least one project.")

# ---------- 6 meetings ----------
with tabs[5]:
    st.subheader("Meeting tracker")
    pj = projects["project"].tolist()
    st.markdown("**Plan next client call** — agenda built from your top open items; drag to reorder.")
    ac1, ac2 = st.columns([1, 2])
    ag_proj = ac1.selectbox("Project", pj, key="ag_proj")
    with ac2:
        agenda = drag_order(mt.suggest_agenda(items, ag_proj), f"agenda_{ag_proj}")
    if ac1.button("➕ Add as meeting"):
        row = {"Date": str(anchor + timedelta(days=now_day)), "Project": ag_proj,
               "Title": f"{ag_proj.split(' – ')[0]} status call", "Attendees": "", "Agenda": "\n".join(agenda),
               "Notes": "", "Decisions": ""}
        ss["meetings"] = pd.concat([ss["meetings"], pd.DataFrame([row])], ignore_index=True)
        ss.pop("med", None)  # drop stale edit-delta so the editor shows the new row
    st.markdown("**Meetings** (edit inline; add rows with the + at the bottom)")
    ss["meetings"] = stretch(st.data_editor, ss["meetings"], num_rows="dynamic", key="med",
                             column_config={"Project": st.column_config.SelectboxColumn(options=pj)})
    st.markdown("**Action items**")
    ss["actions"] = stretch(st.data_editor, ss["actions"], num_rows="dynamic", key="aed", column_config={
        "Project": st.column_config.SelectboxColumn(options=pj), "Done": st.column_config.CheckboxColumn()})
    sc1, sc2 = st.columns(2)
    if sc1.button("💾 Save meetings"):
        if mt.save(ss["meetings"], ss["actions"]):
            st.success("Saved to data/meetings.json")
        else:
            st.error("Could not write file.")
    sc2.download_button("⬇️ Actions (CSV)", ss["actions"].to_csv(index=False), "actions.csv", "text/csv")

# ---------- 7 messages & reminders ----------
with tabs[6]:
    st.subheader("Messages & reminders")
    with st.expander("📇 Address book — add emails so 'Open in email' fills the recipient", expanded=False):
        ss["contacts"] = stretch(st.data_editor, ss["contacts"], num_rows="dynamic", key="ced")
        if st.button("💾 Save contacts"):
            save_contacts(ss["contacts"])
            st.success("Saved to data/contacts.json")
    if ss.get("polish_err"):
        st.error(f"AI unavailable: {ss['polish_err']}")
    drafts = msg.build_drafts(items, projects, ss["contacts"], anchor, sender)
    now_time = "12:00"
    states = {d.id: ("sent" if d.id in ss["sent"] else msg.reminder_state(d, now_day, now_time)) for d in drafts}
    over = [d for d in drafts if states[d.id] in ("overdue", "due today")]
    if over:
        st.warning(f"⏰ {len(over)} message(s) overdue or due today: " + "; ".join(f"{d.to_name} ({d.send_time})" for d in over[:6]))
    st.caption(f"'Today' is simulated as **{DAY_NAMES[now_day]}** 12:00 (change in sidebar). Drafts are generated from the tracker — "
               "review before sending; client drafts may contain internal wording.")
    st.download_button("📅 Download all reminders (.ics) for your calendar", msg.build_ics(drafts, anchor),
                       "tpm_reminders.ics", "text/calendar")
    for d in drafts:
        icon = {"overdue": "🔴", "due today": "🟠", "upcoming": "⚪", "sent": "✅"}[states[d.id]]
        with st.expander(f"{icon} {day_label(d.send_day)} {d.send_time} · {d.kind.replace('_', ' ')} → {d.to_name} · {d.subject[:70]}"):
            dk = d.id + hashlib.md5(d.body.encode()).hexdigest()[:6]  # new tracker data -> fresh draft
            subj = st.text_input("Subject", d.subject, key=f"s_{dk}")
            body = st.text_area("Body", d.body, height=260, key=f"b_{dk}")
            if not d.to_email:
                st.caption("⚠️ No email on file for this recipient — add it in the address book.")
            b1, b2, b3 = st.columns(3)
            d.subject, d.body = subj, body
            b1.link_button("📧 Open in email app", msg.mailto(d))
            b2.button("✨ Polish with AI", key=f"p_{dk}", disabled=not llm.available,
                      on_click=polish, args=(llm, f"b_{dk}"))
            sent = b3.checkbox("Mark as sent", value=d.id in ss["sent"], key=f"m_{d.id}")
            (ss["sent"].add if sent else ss["sent"].discard)(d.id)

# ---------- 8 data ----------
with tabs[7]:
    st.subheader("Parsed data")
    st.markdown("**Projects**")
    stretch(st.dataframe, projects.drop(columns=["target_offset"], errors="ignore"), hide_index=True)
    st.markdown("**Items (with computed fields)**")
    flat = items.copy()
    flat["project"] = flat["project_label"]  # never show the internal "ALL" key
    flat = flat.drop(columns=["project_label"])
    flat["owners"] = flat["owners"].map(", ".join)
    flat["flags"] = flat["flags"].map(", ".join)
    stretch(st.dataframe, flat, hide_index=True)
    st.download_button("⬇️ Cleaned items (CSV)", flat.to_csv(index=False), "items_clean.csv", "text/csv")
    st.markdown("**How due dates are read:** weekday names are mapped to the selected week (Mon = day 0); ISO / "
                "dd/mm/yyyy dates are used as-is; no date → 'TBD' (low urgency). The first date mentioned is the "
                "first deadline; the last is the final one.")
