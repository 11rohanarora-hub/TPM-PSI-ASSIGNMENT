from datetime import date
from pathlib import Path

from tpm_tool.analytics import enrich, enrich_projects
from tpm_tool.parser import parse_tracker

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "project_tracker.xlsx"
ANCHOR = date(2026, 11, 9)  # a Monday


def sample(anchor=ANCHOR, weights=None):
    r = parse_tracker(SAMPLE.read_bytes(), SAMPLE.name)
    it = enrich(r.items, r.projects, weights, anchor)
    return r, it, enrich_projects(r.projects, it, anchor)
