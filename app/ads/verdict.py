"""What to do with one ad: wait / kill / keep / scale.

Sized for the budget we actually run — $300-500 a month, i.e. $10-15 a day
split over a handful of ads — not for a growth team's statistical tests. At
that spend an ad reaches $20 in two or three days, so the thresholds below are
"enough money to have learned something", not significance.

The ceiling is ADS_CAC_CEILING_USD (default $39 = one month of the monthly
plan): a paying user who cost more than their first month is a loss until they
renew, and we have no retention data yet to count on renewals.

Evaluation order, and why it is this order:
  1. wait  — spend < $20 AND impressions < 2,000. Too little to judge anything,
             including a lucky early conversion.
  2. scale — >=1 paying user with CAC <= ceiling, OR >=2 activated users with
             cost per activation <= ceiling/2. Checked BEFORE kill on purpose:
             money (or real activations) beats proxies, so an ad that pays for
             itself is never killed for a low CTR.
  3. kill  — spend >= $30 with 0 signups; OR >= 2,000 impressions with CTR
             < 0.7%; OR spend >= $60 with signups but 0 activated (it buys
             signups that never use the product).
  4. keep  — everything else: spending, some signal, no verdict yet.
"""

import os

WAIT_MAX_SPEND_USD = 20.0
WAIT_MAX_IMPRESSIONS = 2000

KILL_SPEND_WITHOUT_SIGNUP_USD = 30.0
KILL_CTR_MIN_IMPRESSIONS = 2000
KILL_CTR_BELOW_PCT = 0.7
KILL_SPEND_WITHOUT_ACTIVATION_USD = 60.0

SCALE_MIN_PAYING = 1
SCALE_MIN_ACTIVATED = 2

DEFAULT_CAC_CEILING_USD = 39.0

WAIT, KILL, KEEP, SCALE = "wait", "kill", "keep", "scale"


def cac_ceiling() -> float:
    try:
        return float(os.getenv("ADS_CAC_CEILING_USD", "") or DEFAULT_CAC_CEILING_USD)
    except ValueError:
        return DEFAULT_CAC_CEILING_USD


def ctr_pct(clicks: int | None, impressions: int | None) -> float | None:
    if clicks is None or not impressions:
        return None
    return round(clicks / impressions * 100, 2)


def ad_verdict(
    spend: float,
    impressions: int | None,
    clicks: int | None,
    signups: int,
    activated: int,
    paying: int,
    ceiling: float | None = None,
) -> str:
    ceiling = cac_ceiling() if ceiling is None else ceiling
    impressions_n = impressions or 0

    if spend < WAIT_MAX_SPEND_USD and impressions_n < WAIT_MAX_IMPRESSIONS:
        return WAIT

    if paying >= SCALE_MIN_PAYING and spend / paying <= ceiling:
        return SCALE
    if activated >= SCALE_MIN_ACTIVATED and spend / activated <= ceiling / 2:
        return SCALE

    if spend >= KILL_SPEND_WITHOUT_SIGNUP_USD and signups == 0:
        return KILL
    ctr = ctr_pct(clicks, impressions)
    if impressions_n >= KILL_CTR_MIN_IMPRESSIONS and ctr is not None and ctr < KILL_CTR_BELOW_PCT:
        return KILL
    if spend >= KILL_SPEND_WITHOUT_ACTIVATION_USD and signups > 0 and activated == 0:
        return KILL

    return KEEP
