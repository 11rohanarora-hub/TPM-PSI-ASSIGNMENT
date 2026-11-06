"""Plotly figures built from the analysed tables. Each returns None when there is nothing to draw."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from .analytics import DAY_NAMES, IMPACT, PROB, owner_day_matrix, parse_due

import textwrap
from datetime import date, timedelta

import html
import textwrap

RAG_COLORS = {"Red": "#d62728", "Amber": "#ff9f1c", "Green": "#2ca02c"}

def wrap_hover(text, width: int = 48, max_chars: int = 160) -> str:
    """Shorten and line-wrap text for a Plotly tooltip (<br> breaks) so long cells can't stretch it."""
    t = " ".join(str(text or "").split())
    if len(t) > max_chars:
        t = t[:max_chars].rsplit(" ", 1)[0] + "…"
    return "<br>".join(html.escape(line) for line in textwrap.wrap(t, width=width)) or "—"


def _px():
    import plotly.express as px
    return px


PROBS = ["Low", "Medium", "High", "Observed"]
IMPS = ["Low", "Medium", "High", "Critical"]


def heatmap_cells(items: pd.DataFrame, max_ids: int = 4):
    """Pure helper (no plotly): per-cell count, label (item IDs) and hover text, plus unplotted IDs."""
    op = items[items["open"]]
    pr, im = op["probability"].str.strip().str.title(), op["impact"].str.strip().str.title()
    z, text, hover, placed = [], [], [], set()
    for i in IMPS:
        zr, tr, hr = [], [], []
        for p in PROBS:
            cell = op[(pr == p) & (im == i)]
            ids = cell["id"].tolist()
            placed.update(ids)
            zr.append(len(ids))
            label = ids[:max_ids] + ([f"+{len(ids) - max_ids} more"] if len(ids) > max_ids else [])
            tr.append("<br>".join(html.escape(x) for x in label))
            hr.append("<br>".join(f"<b>{html.escape(r['id'])}</b>: {wrap_hover(r['description'], 42, 90)}"
                                  for _, r in cell.iterrows()) or "No items")
        z.append(zr), text.append(tr), hover.append(hr)
    return z, text, hover, [x for x in op["id"] if x not in placed]


def prob_impact_heatmap(items: pd.DataFrame):
    if items.empty:
        return None
    go = __import__("plotly.graph_objects", fromlist=["x"])
    z, text, hover, unplaced = heatmap_cells(items)
    fig = go.Figure(go.Heatmap(z=z, x=PROBS, y=IMPS, text=text, customdata=hover, texttemplate="%{text}",
                               textfont=dict(size=11), colorscale="YlOrRd", showscale=False, xgap=2, ygap=2,
                               hovertemplate="Probability %{x} / Impact %{y}<br>%{customdata}<extra></extra>"))
    fig.update_layout(title="Risk heat-map (open items — hover for details)", xaxis_title="Probability",
                      yaxis_title="Impact", margin=dict(t=50, b=60 if unplaced else 30, l=10, r=10), height=420,
                      hoverlabel=dict(align="left"))
    if unplaced:
        fig.add_annotation(x=0.5, y=-0.28, xref="paper", yref="paper", showarrow=False, font=dict(size=11),
                           text="Not plotted (label not Low/Medium/High/Observed or Low–Critical): "
                                + ", ".join(unplaced[:8]) + ("…" if len(unplaced) > 8 else ""))
    return fig


def priority_bar(items: pd.DataFrame, n: int = 10):
    top = items[items["open"]].head(n)
    top = top.assign(Description=top["description"].map(wrap_hover))
    fig = _px().bar(top.iloc[::-1], x="score", y="id", color="project_label", orientation="h",
                    labels={"project_label": "Project"},
                    hover_data={"Description": True, "score": ":.0f", "project_label": False, "id": False},
                    title="What to do first (priority score)")
    fig.update_layout(margin=dict(t=50, b=30, l=10, r=10), height=360, xaxis_range=[0, 100], yaxis_title="",
                      hoverlabel=dict(align="left"))
    return fig


def timeline(items: pd.DataFrame, projects: pd.DataFrame, anchor: date):
    d = items[items["open"] & items["due_first"].notna()].copy()
    if d.empty:
        return None
    d["start"] = d["due_first"].map(lambda x: pd.Timestamp(anchor + timedelta(days=int(x))))
    d["end"] = d.apply(lambda r: pd.Timestamp(anchor + timedelta(days=int(r["due_last"] if r["due_last"] is not None else r["due_first"]) + 1)), axis=1)
    d["label"] = d["id"] + "  (" + d["score"].round(0).astype(int).astype(str) + ")"
    d["Owner"] = d["owner"].map(wrap_hover)
    d["Deadline"] = d["due"].map(wrap_hover)
    fig = _px().timeline(d, x_start="start", x_end="end", y="label", color="project_label",
                         labels={"project_label": "Project"},
                         hover_data={"Owner": True, "Deadline": True, "start": False, "end": False, "project_label": False},
                         title="Timeline (bar = first → last deadline mentioned)")
    fig.update_layout(hoverlabel=dict(align="left"))
    for _, p in projects.iterrows():
        t = parse_due(p["target"], anchor)["first"]
        if t is not None:
            x = pd.Timestamp(anchor + timedelta(days=t + 1))
            fig.add_shape(type="line", x0=x, x1=x, y0=0, y1=1, yref="paper", line=dict(dash="dash", color="grey"))
            fig.add_annotation(x=x, y=1, yref="paper", text=f"{p['project'].split(' – ')[0]} target",
                               showarrow=False, yanchor="bottom", font=dict(size=10))
    fig.update_layout(margin=dict(t=60, b=30, l=10, r=10), height=140 + 38 * len(d))
    return fig


def owner_heatmap(items: pd.DataFrame):
    m = owner_day_matrix(items)
    if m.empty:
        return None
    fig = _px().imshow(m, text_auto=True, color_continuous_scale="Oranges", aspect="auto",
                       title="Workload: items first due, per person per day")
    fig.update_layout(margin=dict(t=50, b=30, l=10, r=10), height=120 + 45 * len(m), coloraxis_showscale=False)
    return fig


def project_load(projects: pd.DataFrame):
    if projects.empty:
        return None
    fig = _px().bar(projects, x="project", y="open_items", color="rag", color_discrete_map=RAG_COLORS,
                    text="open_items", title="Open items by project (colour = RAG)")
    fig.update_layout(margin=dict(t=50, b=30, l=10, r=10), height=320, xaxis_title="")
    return fig


def type_mix(items: pd.DataFrame):
    d = items[items["open"]]
    if d.empty:
        return None
    c = d.groupby(["type", "project_label"]).size().reset_index(name="n")
    fig = _px().bar(c, x="type", y="n", color="project_label", labels={"project_label": "Project"},
                    title="Items by type")
    fig.update_layout(margin=dict(t=50, b=30, l=10, r=10), height=320, xaxis_title="")
    return fig
