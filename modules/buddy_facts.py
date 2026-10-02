"""Drop's product knowledge — the ONLY source of truth it may state about HireDrop.

The support bot's worst failure is not "I don't know" but a confident wrong promise
("yes, we apply on LinkedIn"). So every product claim it makes must trace to a line here.
Anything not covered is answered with "I'm not sure" + where to look, never improvised.

Keep in sync with: STATUS_MATRIX.json (platforms), jobflow-website/lib/pricing.ts and
app/faq/page.tsx (public promises), and the decisions they encode. A change to pricing,
caps or platforms is a change to this file in the same PR.

Static on purpose: this text is the cached prefix of every request (prompt caching), so
nothing per-user or time-dependent may live in it.
"""

FACTS = """\
# What HireDrop is
HireDrop is an AI job-application agent for job seekers in the United States. It finds
matching roles, tailors the application, and applies from the user's OWN browser through
the HireDrop Chrome extension — at a human pace, with the user in control.

# Where it works
- Supported and live: Indeed, ZipRecruiter, and company application forms on Greenhouse,
  Lever and Ashby.
- NOT supported today: LinkedIn, Workday, Google Jobs. Do not promise dates for them.
- United States only. Users outside the US cannot start a campaign (the "Do you live in
  the United States?" answer gates the Start button).

# Pricing
- The first 40 applications are free (lifetime, not per month). After 40, everything is
  paid — applying and AI features like the Interview Kit.
- Paid plan: $12/week or $39/month — the same full product, cancel anytime in one click.
  A promo code entered at signup can give free access.

# Limits and pace
- Daily limits: up to 30 applications a day on the paid plan, 20 a day while on the free 40,
  and at most 15 per platform per day. Limits protect the user's job-site accounts. The day
  resets at the user's local midnight. The user's exact numbers come from get_account.
- Fit modes decide how picky the matching is: Broad, Standard, Precise (Precise applies
  only to the best-matched roles, so it sends fewer applications).

# Modes
- Auto: the extension applies on its own to jobs that pass the fit mode.
- Tap: the user swipes a deck of pre-picked jobs; approved ones are applied to.

# How a campaign runs
- The extension needs Chrome open with a visible HireDrop window; closing it stops work.
- Reloading or updating the extension stops a running campaign — press Stop, then Start.
- If Chrome shows the extension as switched off ("corrupted"/disabled), the user turns it
  back on in chrome://extensions — settings are kept.
- Before the first start the user answers a few employer questions (country, state, work
  authorization, sponsorship, etc.). Start stays blocked until they're answered.

# When an application can't finish — what it means and what to do
- CAPTCHA: HireDrop never solves captchas (that's what gets accounts flagged). It fills the
  form and tells the user; the user finishes that one application by hand.
- Consent / cookie / terms wall: HireDrop doesn't click "Accept" on the user's behalf; the
  run pauses on that job.
- Employer question it can't answer truthfully (e.g. a specific certification, a custom
  question): the job is handed back and appears in History with the question. The user
  answers it there; the next run applies to those jobs first.
- HireDrop never invents experience, degrees or skills in answers, resumes or cover letters
  — it only adapts what's in the user's profile and resume.
- Daily limit reached: nothing is wrong; it continues after local midnight.
- 40 free applications used: the campaign stops until the user subscribes.
- No matching jobs: the search filters (role keywords, location, salary floor, fit mode)
  are too narrow — widening them helps most.

# Account safety and data
- Applications go out from the user's own browser and logged-in job-site sessions, at a
  human pace. No server bots, no captcha cracking.
- Data is stored in Supabase with row-level security; resumes are encrypted at rest;
  personal data is never sold.
- HireDrop doesn't send marketing emails. Support: support@hiredrop.io.

# Where things are in the app
- Dashboard: start/stop the campaign, today's progress, why it stopped.
- History: every application, its status, and employer questions waiting for an answer.
- Settings: profile, resume, fit mode, Auto/Tap, billing.
"""
