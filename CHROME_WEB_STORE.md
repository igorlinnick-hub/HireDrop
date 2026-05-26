# Chrome Web Store submission package

Developer account: **igor.linnick@gmail.com**
One-time fee: **$5 USD** (paid by Igor at https://chrome.google.com/webstore/devconsole)

---

## Listing fields

### Name (max 75 chars)
HireDrop — Auto-Apply on Indeed

### Short description / summary (max 132 chars)
Auto-fill Indeed applications with your resume + AI-written cover letters. Run, stop, and rate-limit campaigns from one dashboard.

### Category
Productivity

### Language
English

### Detailed description
HireDrop turns a six-step manual job application into a single button click on Indeed.

**How it works**
1. Sign up at hiredrop.io, upload your PDF resume, set your search keywords and writing style.
2. Install this extension and click "Connect Account" — your dashboard session is paired with the extension in one click.
3. Click "Start Campaign" in the popup. HireDrop opens an Indeed search matching your preferences and begins applying to listings one at a time.
4. Each application: extension reads the job title and company, asks the HireDrop backend for an AI-generated cover letter (Claude API), uploads your stored resume, fills the form, and submits.
5. The dashboard tracks every application, with daily and per-platform limits enforced server-side.

**Built-in safeguards**
• Log-normal randomized delays + occasional misclick + Bezier-path mouse emulation so behavior looks human.
• Per-platform daily cap of 50 applications + tiered global daily limits (Free 10, Pro 50, Elite 200).
• Stops the campaign automatically if Indeed shows a captcha and notifies you in the popup.
• A brief warm-up phase before the first action, mimicking how a person settles onto a page.

**Privacy-friendly**
• Auth uses Supabase session tokens stored in chrome.storage.local; nothing leaves your browser except API calls to the HireDrop backend.
• No data is sold to or shared with advertisers.
• Single purpose: automating job applications on Indeed.

**You stay in control**
• Start and stop campaigns manually — the extension never applies in the background unattended.
• Edit your resume, keywords, or writing style at any time on the dashboard.
• Disconnect Indeed in one click from your dashboard settings.

HireDrop is not affiliated with Indeed. You are responsible for ensuring your use of automation complies with Indeed's terms of service.

---

## Single-purpose justification

> HireDrop has a single purpose: automating the user's own job application submissions on indeed.com. The extension reads the job title, company name, and form fields on indeed.com listings, autofills them from the user's stored profile, attaches the user's stored resume, and submits — but only after the user has explicitly clicked "Start Campaign" in the popup or on the HireDrop dashboard. The extension does not read any data unrelated to job applications, does not modify the appearance of the page, and does not perform any non-application action.

---

## Permission justifications

Paste each in the corresponding box during submission.

**`storage`** — Stores the user's Supabase session token, cached user profile (5-minute TTL), today's application count for the badge, and an activity log of the last 50 events. All values are scoped to the local browser via chrome.storage.local and never sent anywhere except the HireDrop backend.

**`tabs`** — Used to (a) open the indeed.com search URL when a campaign starts, (b) listen for that tab's close event so we stop the campaign cleanly, and (c) open the HireDrop dashboard from the popup. We do not enumerate or inspect unrelated tabs.

**`alarms`** — Used to refresh the toolbar badge with the current daily application count once per minute, so the user always sees an up-to-date number without opening the popup.

**`notifications`** — Used to alert the user when a captcha or anti-bot challenge is detected so the user can take over manually, and to confirm a successful application when the dashboard is not open.

**Host permissions**:
- `https://*.indeed.com/*` — Required to read and submit job applications on Indeed (the core function of the extension).
- `https://web-production-db45.up.railway.app/*` — HireDrop's own backend API.
- `https://hiredrop.io/*` — HireDrop's own dashboard, used by the ping.js content script to forward the user's session token from the dashboard into the extension when they click "Connect Account".

---

## Data Use Disclosures (dev console form)

Personal info collected:
- ☑ Name
- ☑ Email address
- ☐ Address
- ☐ Phone number (only if user fills it on their profile)
- ☑ User activity (job applications and timestamps)
- ☐ Web history
- ☐ Location
- ☐ Financial info
- ☐ Health info
- ☐ Authentication info (we store a session token, but it is OAuth-style and only used to talk to our own backend)

Uses:
- ☑ App functionality (autofill + submit applications)
- ☐ Analytics
- ☐ Developer communications
- ☐ Advertising or marketing
- ☐ Personalization
- ☐ Account management (handled by the website, not the extension itself)

Certifications:
- ☑ I do not sell or transfer user data to third parties, outside of the approved use cases.
- ☑ I do not use or transfer user data for purposes that are unrelated to my item's single purpose.
- ☑ I do not use or transfer user data to determine creditworthiness or for lending purposes.

---

## Privacy policy URL
https://hiredrop.io/privacy

## Homepage URL
https://hiredrop.io

## Support email
support@hiredrop.app

---

## Asset checklist

- [x] Icon 16/48/128 — branded "JF" monogram, in `chrome-extension/icons/` (bundled in the zip)
- [x] Small promo tile 440×280 — `store-assets/promo-small-440x280.png`
- [x] Marquee promo tile 1400×560 (optional) — `store-assets/promo-marquee-1400x560.png`
- [ ] Screenshots 1280×800 — at least 1, up to 5. **Igor must capture these from the live app** (they need a logged-in session + the extension loaded). Suggested set:
    1. Extension popup (after auth) showing "Start Campaign" button + today's count
    2. Dashboard with UsageBanner + recent applications
    3. /extension/connect "Extension connected ✓" success state
    4. Indeed listing mid-autofill (with the HireDrop badge visible)
  How: open each view, set the browser window so the content area is 1280×800 (or screenshot then crop/pad to 1280×800), save as PNG.

---

## ZIP build

The extension folder for upload to the dev console is `hiredrop/chrome-extension/`. Zip just that directory's *contents* (not the wrapping folder):

```
cd "hiredrop/chrome-extension"
zip -r ../hiredrop-extension.zip . -x ".DS_Store"
```

The resulting `hiredrop-extension.zip` is what you upload on the "Package" tab.

---

## Submission flow (Igor's actions)

1. Go to https://chrome.google.com/webstore/devconsole using **igor.linnick@gmail.com**.
2. Accept the Developer Agreement.
3. Pay the $5 one-time registration fee.
4. Click "New Item" → upload `hiredrop-extension.zip`.
5. Fill listing fields by copy-paste from the sections above.
6. Fill data-use disclosures by checking the boxes above.
7. Paste each permission justification.
8. Upload icon (auto-detected from manifest) + screenshots + promo tile.
9. Set privacy policy URL + homepage URL + support email.
10. Click "Submit for review".

Review typically takes 5–14 days for an extension with automation + scripting permissions.
