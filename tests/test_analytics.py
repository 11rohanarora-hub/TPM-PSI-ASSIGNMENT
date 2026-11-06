from datetime import date

from tpm_tool.analytics import (coerce_order, DEFAULT_ORDER, day_label, default_anchor, enrich, find_leave, kpis, overloaded,
                                owner_day_matrix, parse_due, parse_owners, slip_effect, weights_from_order)
from tpm_tool.parser import parse_tracker
from tests.helpers import ANCHOR, SAMPLE, sample


def test_parse_due_multiple_days_and_time():
    d = parse_due("Submit Monday 10:30 AM; confirm ETA Monday; Wednesday EOD is an assumption", ANCHOR)
    assert (d["first"], d["last"], d["time"]) == (0, 2, "10:30")


def test_parse_due_pm_and_eod():
    assert parse_due("Monday – report by 1:00 PM", ANCHOR)["time"] == "13:00"
    assert parse_due("Tuesday EOD – handover", ANCHOR)["time"] == "17:00"
    assert parse_due("12:15 am", ANCHOR)["time"] == "00:15"


def test_parse_due_does_not_match_inside_words():
    assert parse_due("Monitoring next month; sunny outlook", ANCHOR)["first"] is None


def test_parse_due_real_dates_become_offsets():
    assert parse_due("2026-11-11", ANCHOR)["first"] == 2
    assert parse_due("due 2026-11-08", ANCHOR)["first"] == -1  # overdue
    assert parse_due("TBD", ANCHOR)["first"] is None and parse_due("", ANCHOR)["first"] is None


def test_owner_parsing():
    assert parse_owners("Meera + Sana; Arjun if backend/data path implicated") == ["Meera", "Sana", "Arjun"]
    assert parse_owners("TPM + Sagar business owner + technical owners") == ["TPM", "Sagar business owner", "technical owners"]
    assert parse_owners("") == [] and parse_owners("Ann, Bob & Cy") == ["Ann", "Bob", "Cy"]


def test_leave_detection():
    assert find_leave(["Meera is on leave Wednesday–Friday, creating a risk"]) == {"Meera": (2, 4)}
    assert find_leave(["Raj is out Tue"]) == {"Raj": (1, 1)}
    assert find_leave(["nothing here"]) == {}


def test_weights_sum_to_one_and_follow_order():
    w = weights_from_order(DEFAULT_ORDER)
    assert abs(sum(w.values()) - 1) < 1e-9 and w["Impact"] > w["Resource contention"]
    assert weights_from_order(["Probability"])["Probability"] > weights_from_order(["Impact"])["Probability"]


def test_sample_ranking_and_conflicts():
    r, it, pr = sample()
    assert it.loc[0, "id"] == "KAV-02"  # critical + observed + gate
    assert it["score"].between(0, 100).all() and it["rank"].tolist() == list(range(1, 10))
    conflicts = dict(zip(it["id"], it["leave_conflict"]))
    assert "Meera" in conflicts["KAV-04"] and "Meera" in conflicts["KAV-02"] and conflicts["LED-02"] == ""


def test_gate_detection():
    r, it, _ = sample()
    g = dict(zip(it["id"], it["gate"]))
    assert g["KAV-02"] and g["KAV-03"] and not g["LED-03"]


def test_drag_preferences_change_the_ranking():
    _, a, _ = sample(weights=weights_from_order(["Release gate"] + DEFAULT_ORDER))
    _, b, _ = sample(weights=weights_from_order(["Urgency (due date)", "Resource contention"] + DEFAULT_ORDER))
    assert a["score"].tolist() != b["score"].tolist()


def test_missing_due_dates_do_not_crash_anything():
    r = parse_tracker(SAMPLE.read_bytes(), "x.xlsx")
    r.items["due"] = "TBD"
    it = enrich(r.items, r.projects, anchor=ANCHOR)
    assert it["due_first"].isna().all() or all(v is None for v in it["due_first"])
    assert owner_day_matrix(it).empty
    assert day_label(None) == "TBD"
    assert slip_effect(it, r.projects, "KAV-01", 2)["note"]


def test_closed_items_leave_priority_queue():
    r = parse_tracker(SAMPLE.read_bytes(), "x.xlsx")
    r.items.loc[r.items.id == "KAV-02", "status"] = "Resolved"
    it = enrich(r.items, r.projects, anchor=ANCHOR)
    assert it.set_index("id").loc["KAV-02", "score"] == 0 and it.loc[0, "id"] != "KAV-02"
    assert kpis(it, r.projects)["open_items"] == 8


def test_workload_matrix_flags_arjun_on_tuesday():
    _, it, _ = sample()
    assert any(s.startswith("Arjun has 3 items first due on Tue") for s in overloaded(owner_day_matrix(it)))
    assert "TPM" not in owner_day_matrix(it).index


def test_slip_breaches_target():
    r, it, pr = sample()
    res = slip_effect(it, r.projects, "KAV-04", 2, ANCHOR)  # Wed -> Fri vs Thursday target
    assert res["new_due_label"] == "Fri" and "missed by 1" in res["breaches"][0]
    assert slip_effect(it, r.projects, "KAV-04", 0, ANCHOR)["breaches"] == []


def test_slip_unknown_item():
    r, it, _ = sample()
    try:
        slip_effect(it, r.projects, "NOPE", 1)
    except KeyError:
        return
    raise AssertionError("expected KeyError")


def test_default_anchor_is_a_monday():
    assert default_anchor(date(2026, 11, 6)) == date(2026, 11, 9)   # Friday -> next Monday
    assert default_anchor(date(2026, 11, 9)) == date(2026, 11, 9)   # Monday -> itself
    assert default_anchor(date(2026, 11, 11)) == date(2026, 11, 9)  # Wednesday -> this Monday


def test_day_label_edge_cases():
    assert day_label(0) == "Mon" and day_label(8) == "Tue (+1w)" and day_label(-2) == "2d overdue"


def test_coerce_order_handles_every_shape_the_component_may_return():
    base = ["a", "b", "c"]
    assert coerce_order(base, ["c", "a", "b"]) == ["c", "a", "b"]                 # normal reorder
    assert coerce_order(base, [{"header": None, "items": ["b", "c", "a"]}]) == ["b", "c", "a"]  # container form
    assert coerce_order(base, None) == base and coerce_order(base, []) == base    # nothing returned
    assert coerce_order(base, ["x", "y", "z"]) == base                            # stale state, different items
    assert coerce_order(base, [1, 2, 3]) == base and coerce_order(base, "abc") == base
    assert coerce_order(["a", "b"], ["a", "a"]) == ["a", "b"]                     # duplicates rejected
