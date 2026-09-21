"""Tailoring renames the candidate's own work. It never adds someone else's.

Read live on 2026-09-21 against a Field Marketing posting with ATS keywords
["demand generation", "field marketing", "Salesforce", "Marketo", "ABM"], the tailored
resume of a project manager came back with:

    SKILLS: ... ABM campaign coordination, Salesforce, Marketo, field marketing
    execution, demand generation program support

and the headline retitled from "Project Manager" to "Field Marketing Manager". None of
it was in the original. The candidate had never opened Salesforce or Marketo.

The cause was a prompt that contradicted itself. "Do NOT fabricate" sat in the rules,
while the keyword block above it said "Include ALL of them naturally in context". The
more specific, more insistent instruction won — as it always will.

These pin the resolution: the keyword rule is explicitly subordinate, product names are
called out by name (they are how a tailored resume becomes a false one), and the headline
title is protected along with the dated ones. Prompt text, because the failure was in the
prompt — a live model call here would cost money on every CI run and still only sample
one of its outputs.
"""

import inspect

from modules import ai_resume_tailor


def _prompt_source() -> str:
    return inspect.getsource(ai_resume_tailor.tailor_resume)


def test_keywords_are_used_only_where_the_resume_backs_them():
    src = _prompt_source()
    assert "ONLY where the candidate's own resume already backs it" in src
    assert "LEAVE THE KEYWORD OUT" in src
    # The old wording is what caused it. It must not come back.
    assert "Include ALL of them" not in src


def test_no_invention_outranks_the_keyword_list():
    src = _prompt_source()
    assert "OUTRANKS" in src, "the precedence between the two rules must be explicit"
    assert "the keyword loses" in src


def test_product_names_are_banned_by_name():
    src = _prompt_source()
    assert "Never add a TOOL, PLATFORM or PRODUCT NAME" in src
    # The examples matter: they are what the model pattern-matches against.
    for example in ("Salesforce", "Marketo"):
        assert example in src, example


def test_the_headline_title_is_protected_too():
    src = _prompt_source()
    assert "including the headline title" in src


def test_metrics_are_not_invented():
    src = _prompt_source()
    assert "Do NOT invent metrics" in src
