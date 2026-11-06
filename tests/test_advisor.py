from tpm_tool import advisor
from tpm_tool.llm import LLM, LLMError, scrub
from tests.helpers import sample


class FakeLLM(LLM):
    def __init__(self, reply=None, fail=False):
        super().__init__(token="x", model="fake/model")
        self.reply, self.fail, self.calls = reply, fail, []

    def complete(self, system, user=None, history=None, **kw):
        self.calls.append((system, user))
        if self.fail:
            raise LLMError("boom")
        return self.reply


def test_scrub_think_blocks():
    assert scrub("<think>secret</think>Answer") == "Answer"
    assert scrub("partial reasoning</think>Final") == "Final"
    assert scrub("plain") == "plain" and scrub(None) == ""


def test_llm_unavailable_without_token(monkeypatch=None):
    import os
    for k in ("HF_TOKEN", "HUGGINGFACEHUB_API_TOKEN", "HF_BASE_URL"):
        os.environ.pop(k, None)
    l = LLM()
    assert not l.available
    try:
        l.complete("s", "u")
    except LLMError:
        return
    raise AssertionError("expected LLMError")


def test_offline_answers():
    r, it, pr = sample()
    a = lambda q: advisor.offline_answer(q, it, pr)
    assert "KAV-02" in a("What is KAV-02?") and "No item with ID ZZ-9" in a("tell me about zz-9")
    assert "KAV-02" in a("what should I do first?")
    assert "Meera on leave" in a("who is on leave")
    assert a("how many items for ledger").startswith("4 item(s)")
    assert "Meera" in a("what is due Tuesday for Meera") and "KAV-05" in a("what is due Tuesday for Meera")
    assert "Red" in a("project status")
    assert "not in the tracker" in a("what's the weather in Paris?")
    assert "Nothing in the tracker matches" in a("what is due Friday for Dev")


def test_context_retrieval_limits_rows():
    r, it, pr = sample()
    ctx = advisor.build_context(pr, it, question="duplicate records", k=3)
    assert ctx.count("[LED-01]") == 1 and ctx.count("\n[") == 3


def test_llm_used_when_available_and_grounded():
    r, it, pr = sample()
    f = FakeLLM("Do KAV-02 first [KAV-02]")
    ans, src = advisor.chat_answer("what first?", [], it, pr, f)
    assert ans.startswith("Do KAV-02") and src == "fake/model"
    assert "TRACKER DATA" in f.calls[0][0] and "KAV-02" in f.calls[0][0]


def test_llm_failure_falls_back_to_rules():
    r, it, pr = sample()
    ans, src = advisor.chat_answer("what should I do first", [], it, pr, FakeLLM(fail=True))
    assert src == "rule-based" and "KAV-02" in ans and "LLM unavailable" in ans
    md, src = advisor.recommendations_markdown(it, pr, FakeLLM(fail=True))
    assert src == "rule-based" and "KAV-02" in md


def test_recommendations_have_effects():
    r, it, pr = sample()
    recs = advisor.rule_recommendations(it, pr)
    assert recs[0]["title"].startswith("[KAV-02]") and all(x["effect"] for x in recs)
    assert any(x["title"] == "Rebalance workload" for x in recs)
