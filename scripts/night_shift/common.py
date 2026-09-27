"""Rules every night-shift board adapter shares (Greenhouse in executor.py, Ashby in
ashby.py). One place, so the two boards cannot disagree about what a demographic
question is or which honest answer disqualifies the candidate."""

import re

# Voluntary self-identification questions, and the neutral way out of them. We decline
# these on principle rather than let a model infer a person's gender or race from a CV.
_DEMOGRAPHIC_Q = re.compile(
    r"gender|race|ethnic|sexual orientation|lgbt|transgender|disability|veteran"
    r"|self.?identif|demographic|pronoun",
    re.I,
)
_DECLINE_OPT = re.compile(
    r"don'?t wish|do not wish|decline|prefer not|rather not|not to (?:answer|disclose)"
    r"|no answer|not wish to (?:answer|identify)",
    re.I,
)


def log(msg: str) -> None:
    print(msg, flush=True)


# KNOCKOUT: a screener whose honest answer rules the candidate out. "Are you located in
# X / can you work from our office / do you have N years" is the employer's filter, not
# a preference — answering "no" and sending anyway spends a daily slot on a certain
# rejection. Sponsorship questions are the inverse ("No" is the good answer) and never
# knock out.
_HARD_Q = re.compile(
    r"are you (?:currently )?(?:located|based|living)|able to (?:work|commute|relocate)"
    r"|do you (?:have|possess).{0,40}(?:years|experience|degree|license|certification)"
    r"|legally (?:authorized|eligible)|eligible to work|willing to relocate"
    r"|can you (?:work|start|commute)",
    re.I,
)
_NEGATIVE_A = re.compile(r"^\s*(no|nope|n/a|not\b|i am not|i'm not|i do not|i don't)\b", re.I)


def is_knockout(question: str, answer: str) -> bool:
    if not question or not answer or re.search(r"sponsor|visa", question, re.I):
        return False
    return bool(_HARD_Q.search(question) and _NEGATIVE_A.match(answer))
