import io

import pandas as pd

from tpm_tool.parser import ParseError, client_of, parse_tracker
from tests.helpers import SAMPLE


def raises(exc, fn, *a):
    try:
        fn(*a)
    except exc as e:
        return str(e)
    raise AssertionError(f"{exc.__name__} not raised")


def test_sample_workbook_structure():
    r = parse_tracker(SAMPLE.read_bytes(), "project_tracker.xlsx")
    assert r.sheets_used == {"Summary": "projects", "Risks and Issues": "items"}
    assert len(r.projects) == 2 and len(r.items) == 9
    assert r.projects["rag"].tolist() == ["Red", "Amber"]
    assert r.warnings == []


def test_footnote_and_banner_rows_are_not_items():
    r = parse_tracker(SAMPLE.read_bytes(), "x.xlsx")
    assert not r.items["id"].str.contains("Probability").any()
    assert set(r.items["id"]) == {"KAV-01", "KAV-02", "KAV-03", "KAV-04", "KAV-05", "X-01", "LED-01", "LED-02", "LED-03"}


def test_short_project_names_resolve_and_both_is_cross_project():
    r = parse_tracker(SAMPLE.read_bytes(), "x.xlsx")
    by = dict(zip(r.items["id"], r.items["project"]))
    assert by["KAV-01"] == "Kavach – Nirmal Finance"
    assert by["LED-03"] == "Ledger – Sagar Publications"


def test_csv_with_banner_and_different_column_order():
    csv = ("My tracker,,,,\n,,,,\nOwner,Impact,Probability,Description,ID,Project\n"
           "Ann,High,Medium,Slow API,R-1,Alpha\nBob,Critical,Observed,Data loss,R-2,Alpha\n,,,,,\nnote: draft,,,,,\n")
    r = parse_tracker(csv.encode(), "t.csv")
    assert r.items["id"].tolist() == ["R-1", "R-2"]
    assert r.projects["project"].tolist() == ["Alpha"]
    assert r.projects["rag"].tolist() == ["Red"]  # derived: has a Critical item


def test_rag_derivation_levels():
    mk = lambda imp, prob: parse_tracker(f"ID,Project,Description,Impact,Probability\nA-1,P,thing,{imp},{prob}\n".encode(), "a.csv").projects.rag[0]
    assert mk("Low", "Low") == "Green"
    assert mk("High", "Low") == "Amber"
    assert mk("Medium", "Observed") == "Amber"
    assert mk("Critical", "Low") == "Red"


def test_missing_ids_get_generated_and_duplicates_warned():
    r = parse_tracker(b"ID,Project,Description,Impact\n,P,one,High\n,P,two,Low\nX,P,three,Low\nX,P,four,Low\n", "a.csv")
    assert r.items["id"].tolist()[:2] == ["ITEM-1", "ITEM-2"]
    assert any("Duplicate IDs" in w for w in r.warnings) and any("no ID" in w for w in r.warnings)


def test_unrecognised_file_gives_friendly_error():
    assert "No item/risk table" in raises(ParseError, parse_tracker, b"foo,bar\n1,2\n", "a.csv")


def test_unsupported_extension_and_corrupt_excel():
    assert "Unsupported" in raises(ParseError, parse_tracker, b"x", "notes.pdf")
    assert "Could not open" in raises(ParseError, parse_tracker, b"not an excel file", "bad.xlsx")


def test_empty_csv():
    raises(ParseError, parse_tracker, b"", "empty.csv")


def test_header_only_has_no_rows():
    assert "no data rows" in raises(ParseError, parse_tracker, b"ID,Project,Description,Impact\n", "h.csv")


def test_client_of():
    assert client_of("Kavach – Nirmal Finance") == "Nirmal Finance"
    assert client_of("Solo") == "Solo"


def test_roundtrip_excel_written_by_pandas():
    df = pd.DataFrame({"ID": ["A-1"], "Project": ["Z"], "Description": ["d"], "Impact": ["High"], "Owner": ["Sam"]})
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    r = parse_tracker(buf.getvalue(), "w.xlsx")
    assert r.items.loc[0, "owner"] == "Sam"


def test_ragged_csv_never_drops_rows():
    csv_text = "Banner\nID,Project,Description,Impact,Owner,Due\n" + "\n".join(f"R-{i},P,thing {i},High,Ann,Monday" for i in range(25))
    r = parse_tracker(csv_text.encode(), "r.csv")
    assert len(r.items) == 25


def test_csv_with_bom_quotes_and_commas_in_cells():
    txt = '\ufeffID,Project,Description,Impact\nA-1,P,"Slow, flaky API",High\n'
    r = parse_tracker(txt.encode("utf-8"), "b.csv")
    assert r.items.loc[0, "description"] == "Slow, flaky API" and r.items.loc[0, "id"] == "A-1"
