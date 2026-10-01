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
    assert "NEVER invent" in p
    for word in ("certifications", "licences", "degrees", "clearances"):
        assert word in p, word
    # Hard requirements that are absent get an honest no — favor ends where facts end.
    assert "honest no" in p


# --- 2026-09-21: the ban had a hole big enough to walk a whole tool through ---------
#
# The old list banned employers/titles/dates/certs/licences/degrees/clearances. TOOLS
# were not on it, and "treat familiar with as yes" sat two lines above. Read live that
# day against a real Greenhouse screener ("Being an Airtable user is highly preferred,
# outline your level of experience"), the model wrote: "I've used Airtable regularly for
# project tracking... building bases, setting up views and filters, and using
# automations." Airtable appears nowhere in that resume. It also invented a whole
# anecdote for an AI-usage question, down to the scenario and the time saved.
#
# Igor's line, same day: "мы же не выдумываем а адаптируем" — we adapt, we don't invent.
# These pin the distinction, because it is the one a prompt edit is most likely to blur.


def test_tools_and_anecdotes_are_named_as_uninventable():
    p = _system_prompt()
    low = p.lower()
    assert "tools and software" in low, "the tool ban must be explicit — it was the hole"
    assert "anecdotes" in low or "did not happen" in low
    assert "metrics" in low or "numbers" in low


def test_the_adapt_versus_invent_line_is_drawn_in_words():
    p = _system_prompt()
    assert "ADAPT" in p and "INVENT" in p
    assert "traceable to the resume" in p


def test_an_absent_tool_gets_the_ANALOGY_not_a_bare_no():
    # The alternative to inventing must be spelled out, or "answer in the candidate's
    # favour" fills the vacuum by itself. Igor, 09-21: a bare "no" throws away a real
    # qualification — if the candidate has done the same KIND of work with another
    # tool, the answer is the analogy. Read live after this change: Airtable →
    # "I haven't used Airtable, but I've run project tracking and intake workflows in
    # Jira… the relational, database-driven side is familiar ground"; Asana → the same
    # move; Kubernetes → an honest no with NO analogy, because a project manager has
    # no transferable claim on running infrastructure. The rule has to produce both.
    p = _system_prompt()
    low = p.lower()
    assert "ANALOGY" in p, "the analogy must be named as the move, not implied"
    assert "closest thing" in low
    assert "haven't used" in low
    # A tool is never claimed; a skill exercised elsewhere always counts. Both halves,
    # or the rule collapses into one of the two failure modes.
    assert "never claimed" in low
    assert "always counts" in low
    # And it must not read as an apology — hedging loses the application at the door.
    assert "never hedge" in low


def test_it_still_forbids_ai_tells_and_hedging():
    p = _system_prompt()
    assert "No buzzwords" in p
    assert "hedging" in p


# ---------------------------------------------------------------------------
# Legal work status: the profile answers, or nobody does (2026-09-21)
#
# content.js refuses to guess a sponsorship/authorization knockout and punts to the AI.
# The AI then guessed anyway — "No, sponsorship not required" off a US address — for the
# 34 of 37 profiles that have the fields unset. A wrong answer here is a false statement
# on an employment form, made under the candidate's name.

from modules.ai_question_answer import _status_from_profile  # noqa: E402


def test_silent_profile_refuses_to_answer_a_status_question():
    for q in (
        "Will you now or in the future require visa sponsorship in order to work in the US?",
        "Are you authorized to work in the country for which you applied?",
        "Do you have permanent work authorization to work in the U.S?",
    ):
        assert _status_from_profile(q, {}, ["Yes", "No"]) == "", q


def test_profile_answers_are_used_verbatim():
    q_sponsor = "Will you now or in the future require visa sponsorship?"
    assert _status_from_profile(q_sponsor, {"needs_sponsorship": True}, ["Yes", "No"]) == "Yes"
    assert _status_from_profile(q_sponsor, {"needs_sponsorship": False}, ["Yes", "No"]) == "No"
    q_auth = "Are you authorized to work in the US?"
    assert _status_from_profile(q_auth, {"work_authorized_us": True}, ["Yes", "No"]) == "Yes"
    assert _status_from_profile(q_auth, {"work_authorized_us": False}, ["Yes", "No"]) == "No"


def test_a_multi_visa_dropdown_is_handed_back_not_guessed():
    # GitLab lists seven visa types; a boolean cannot choose among them.
    opts = ["No", "Yes, EU Blue Card", "Yes, F-1 Visa OPT (USA)"]
    assert (
        _status_from_profile("Will you require sponsorship?", {"needs_sponsorship": True}, opts)
        == ""
    )


def test_non_status_questions_are_untouched():
    # None means "not my business, carry on to the model".
    for q in (
        "Have you ever worked for Figma before?",
        "Why do you want to work at Discord?",
        "What is your notice period?",
    ):
        assert _status_from_profile(q, {}, ["Yes", "No"]) is None, q


def test_work_status_for_another_country_is_refused_not_answered_from_the_us_flag():
    """Live 09-30: "(Canada)" role, US-only profile, and the answer going out was Yes."""
    from modules.ai_question_answer import _status_from_profile

    profile = {"work_authorized_us": True, "needs_sponsorship": False}
    opts = ["Yes", "No"]
    assert (
        _status_from_profile("Are you legally authorized to work in Canada?", profile, opts) == ""
    )
    assert (
        _status_from_profile("Do you require visa sponsorship to work in the UK?", profile, opts)
        == ""
    )
    # The US question, and one that names no country at all, are answered as before.
    assert (
        _status_from_profile(
            "Are you legally authorized to work in the United States?", profile, opts
        )
        == "Yes"
    )
    assert (
        _status_from_profile(
            "Will you now or in the future require sponsorship for employment visa status?",
            profile,
            opts,
        )
        == "No"
    )
    # A US question that mentions other countries only as visa examples stays a US question.
    assert (
        _status_from_profile(
            "Are you authorized to work in the US without sponsorship (e.g. TN for Canada/Mexico)?",
            profile,
            opts,
        )
        == "No"
    )


# Labels copied from the live Muck Rack form (Greenhouse), dry walk 2026-09-30.
HUMAN_ATTESTATION = (
    "I hereby confirm that I am a real human being and not an automated bot or artificial"
    " intelligence. I understand that this attestation is part of the hiring process and"
    " affirm that all information I have provided in my application and communications has"
    " been created and submitted by me, personally."
)


def _refused_without_a_model(question, options=None, profile=None):
    from unittest.mock import patch

    from modules import ai_question_answer as aq

    with (
        patch.object(aq, "ANTHROPIC_API_KEY", "k"),
        patch.object(aq, "get_anthropic_client") as client,
    ):
        out = aq.answer_screener_question(question, job={}, profile=profile or {}, options=options)
        client.assert_not_called()
    return out


def test_a_program_never_attests_to_being_the_human():
    """The filler picked "I Agree" here — an automated tool certifying it is not one."""
    assert _refused_without_a_model(HUMAN_ATTESTATION, ["I Agree", "I Disagree"]) == ""
    for q in (
        "Are you a robot?",
        "Are you a real human?",
        "I certify that this application was completed by me personally.",
        "Did you use AI to complete this application?",
        "Did you use AI or other tools to help write your application?",
        "Were any of your responses AI-generated?",
        "Was this application written with the help of AI?",
        "Please confirm you did not use ChatGPT for these answers.",
        "I confirm these answers are my own work and were written without the use of AI tools.",
        "By submitting, I attest that my responses were not generated by artificial intelligence.",
        "This application must be completed without AI assistance. Do you agree?",
        "Have you used ChatGPT or similar to write your cover letter?",
    ):
        assert _refused_without_a_model(q, ["Yes", "No"]) == "", q


def test_pronouns_and_how_a_name_is_said_are_not_inferred():
    assert _refused_without_a_model("What are your personal pronouns?") == ""
    assert (
        _refused_without_a_model(
            "Pronouns", ["He/him", "She/her", "They/them", "Prefer not to say"]
        )
        == "Prefer not to say"
    )
    assert _refused_without_a_model("What is the phonetic spelling of your name?") == ""
    assert _refused_without_a_model("How do you pronounce your name?") == ""


def test_ordinary_questions_about_ai_and_people_still_reach_the_model():
    """The refusal is for attestations — not for every question that mentions AI."""
    from modules.ai_question_answer import _only_the_person

    for q in (
        "Do you have experience with artificial intelligence tools?",
        "Describe how you have used AI in your marketing work.",
        "Have you used AI tools to speed up reporting?",
        "How have you used AI to answer customer questions?",
        "Do you have experience building AI applications?",
        "Our team uses AI heavily. Are you comfortable with that?",
        "I certify that the information I provided is true and complete.",
        "Tell us about a project you completed on your own initiative.",
        "Are you comfortable managing a team of five people?",
        "Why do you want to work at Humane?",
        "Do you have experience with marketing automation tools?",
        "Applicant Privacy Acknowledgement",
    ):
        assert _only_the_person(q, []) is None, q
