"""The screener-answer policy is a product decision — pin it so a prompt edit can't
silently flip it.

Igor, 2026-09-11: screeners are a pass/fail FILTER, not an interview. Near-miss
experience rounds UP (2 years asked for 3 = yes); adjacent experience counts. The other
half is just as binding: employers, titles, certifications, licenses, degrees, clearances
are never invented — a faked credential surfaces at the interview and burns the candidate.
"""

from modules.ai_question_answer import _system_prompt


def test_favors_the_candidate_on_near_misses():
    p = _system_prompt()
    assert "round" in p.lower() and "up" in p.lower()
    assert "transferable" in p.lower()
    assert "more favorable to the candidate" in p


def test_fabrication_stays_banned_and_names_the_dangerous_specifics():
    p = _system_prompt()
    assert "NEVER fabricate" in p
    for word in ("certifications", "licenses", "degrees", "clearances"):
        assert word in p, word
    # Hard requirements that are absent get an honest no — favor ends where facts end.
    assert "honest no" in p


def test_it_still_forbids_ai_tells_and_hedging():
    p = _system_prompt()
    assert "No buzzwords" in p
    assert "hedging" in p
