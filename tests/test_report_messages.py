import io
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

from tpm_tool import meetings as mt, messages as msg, report as rp
from tests.helpers import ANCHOR, sample


def test_report_sections_order_and_content():
    r, it, pr = sample()
    rep = rp.build_report(pr.iloc[0], it, anchor=ANCHOR, order=["Next steps this week", "Executive summary"])
    assert [s for s, _ in rep["sections"]] == ["Next steps this week", "Executive summary"]
    md = rp.to_markdown(rp.build_report(pr.iloc[0], it, anchor=ANCHOR))
    assert "RED" in md and "KAV-02" in md and "LED-01" not in md # cross-project item included


def test_client_audience_hides_internal_content():
    r, it, pr = sample()
    md = rp.to_markdown(rp.build_report(pr.iloc[0], it, anchor=ANCHOR, audience="client"))
    assert "Decisions & controls" not in md and "⚠" not in md  # internal warnings stay internal
    assert "⚠" in rp.to_markdown(rp.build_report(pr.iloc[0], it, anchor=ANCHOR, audience="internal"))


def test_completed_section_uses_meeting_actions():
    r, it, pr = sample()
    acts = pd.DataFrame([{"Project": pr.iloc[1]["project"], "Meeting": "m", "Action": "Sent report", "Owner": "TPM", "Due": "Mon", "Done": True}])
    md = rp.to_markdown(rp.build_report(pr.iloc[1], it, mt.empty_meetings(), acts, ANCHOR))
    assert "Sent report (TPM)" in md


def test_docx_is_valid_zip_with_content():
    r, it, pr = sample()
    reps = [rp.build_report(p, it, anchor=ANCHOR) for _, p in pr.iterrows()]
    data = rp.to_docx(reps)
    z = zipfile.ZipFile(io.BytesIO(data))
    xml = z.read("word/document.xml").decode()
    assert "Weekly Status" in xml and "KAV-02" in xml and "LED-02" in xml


def test_drafts_cover_clients_owners_and_stakeholders():
    r, it, pr = sample()
    ds = msg.build_drafts(it, pr, msg.contact_template(it, r.projects), ANCHOR)
    kinds = {d.kind for d in ds}
    assert kinds == {"client_update", "owner_nudge", "stakeholder"}
    names = {d.to_name for d in ds}
    assert {"Meera", "Arjun", "COO", "Group CTO"} <= names and "TPM" not in names
    coo = next(d for d in ds if d.to_name == "COO")
    assert (coo.send_day, coo.send_time) == (0, "13:00")  # "report by 1:00 PM" Monday
    assert [(d.send_day, d.send_time) for d in ds] == sorted((d.send_day, d.send_time) for d in ds)
    meera = next(d for d in ds if d.to_name == "Meera")
    assert "on leave" in meera.body


def test_contact_lookup_and_mailto_encoding():
    r, it, pr = sample()
    ct = msg.contact_template(it, r.projects)
    ct.loc[ct.Name == "Meera", "Email"] = "meera@example.com"
    ds = msg.build_drafts(it, pr, ct, ANCHOR)
    m = next(d for d in ds if d.to_name == "Meera")
    assert m.to_email == "meera@example.com"
    url = msg.mailto(m)
    assert url.startswith("mailto:meera%40example.com?subject=") and "\n" not in url and " " not in url
    assert next(d for d in ds if d.to_name == "Arjun").to_email == ""  # missing -> blank, not crash


def test_reminder_states():
    r, it, pr = sample()
    d = msg.build_drafts(it, pr, None, ANCHOR)[0]  # Mon 09:00
    assert msg.reminder_state(d, 0, "12:00") == "overdue"
    assert msg.reminder_state(d, 0, "08:00") == "due today"
    assert msg.reminder_state(d, 3, "08:00") == "overdue" and msg.reminder_state(d, -1, "08:00") == "upcoming"


def test_ics_is_wellformed():
    r, it, pr = sample()
    ds = msg.build_drafts(it, pr, None, ANCHOR)
    ics = msg.build_ics(ds, ANCHOR)
    assert ics.startswith("BEGIN:VCALENDAR") and ics.rstrip().endswith("END:VCALENDAR")
    assert ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT") == len(ds)
    assert "DTSTART:20261109T090000" in ics and "\r\n" in ics
    assert len({d.id for d in ds}) == len(ds)  # stable unique UIDs
    assert msg.build_ics([], ANCHOR).count("VEVENT") == 0


def test_meetings_roundtrip_and_corrupt_file():
    with tempfile.TemporaryDirectory() as t:
        p = Path(t) / "sub" / "m.json"
        m = pd.DataFrame([{c: "x" for c in mt.MEETING_COLS}])
        a = pd.DataFrame([{"Project": "P", "Meeting": "m", "Action": "do", "Owner": "Ann", "Due": "Mon", "Done": True}])
        assert mt.save(m, a, p)
        m2, a2 = mt.load(p)
        assert m2.shape == (1, 7) and bool(a2.loc[0, "Done"]) is True
        p.write_text("{not json")
        m3, a3 = mt.load(p)
        assert m3.empty and a3.empty
        assert mt.load(Path(t) / "missing.json")[0].empty


def test_agenda_from_top_items():
    r, it, pr = sample()
    ag = mt.suggest_agenda(it, pr.iloc[0]["project"], limit=3)
    assert ag[0].startswith("[KAV-02]") and ag[-1].startswith("Confirm weekly reporting") and len(ag) == 4
    assert mt.suggest_agenda(it.iloc[0:0], "x") == []
