// Fixture test for the EDGE PILL (pill.js) — what it shows, and where it refuses to show.
//
//   node <repo>/jobflow/chrome-extension/tests/edge-pill.test.js
//
// The two decisions pinned here are the ones that could do harm if they drifted:
//   1. The pill never mounts in the campaign's own tab or window. content.js drives that
//      page with CDP clicks at coordinates; a pill opened by a dispatched hover would sit
//      under the next click.
//   2. The count is today's, in the user's local day — a todayCount left over from
//      yesterday must read 0, the same rule GET_STATUS applies — and the action follows
//      the state: a waiting wall outranks a running campaign (the human is the blocker).

const { hdPillView, hdPillHidden } = require("../pill.js");

let failures = 0;
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    failures += 1;
    console.error(`FAIL ${name}\n  expected: ${JSON.stringify(expected)}\n  actual:   ${JSON.stringify(actual)}`);
  } else {
    console.log(`ok   ${name}`);
  }
}

const TODAY = "2026-09-30";
const pick = (v) => ({ phase: v.phase, done: v.done, cap: v.cap, pct: v.pct, line: v.line, label: v.label, action: v.action });

// --- what it shows ---------------------------------------------------------------------

check(
  "running: today's count over the daily cap, current company, Stop",
  pick(hdPillView({
    today: TODAY, todayDate: TODAY, todayCount: 12, campaignRunning: true,
    campaignCaps: { dailyTotal: 30 }, currentJob: { title: "Product Designer", company: "Northwind Labs" },
  })),
  { phase: "running", done: 12, cap: 30, pct: 40, line: "Applying · Northwind Labs", label: "Stop", action: "stop" },
);

check(
  "yesterday's todayCount reads 0 (local day rollover, same rule as GET_STATUS)",
  hdPillView({ today: TODAY, todayDate: "2026-09-29", todayCount: 27, campaignRunning: false }).done,
  0,
);

check(
  "no caps stored yet → background's DEFAULT_DAILY_TOTAL (30)",
  hdPillView({ today: TODAY, todayDate: TODAY, todayCount: 3 }).cap,
  30,
);

check(
  "a waiting captcha outranks a running campaign → Open focuses the wall",
  pick(hdPillView({
    today: TODAY, todayDate: TODAY, todayCount: 12, campaignRunning: true,
    captchaWaiting: { kind: "captcha", tabId: 7, url: "https://boards.greenhouse.io/x" },
  })),
  { phase: "needs", done: 12, cap: 30, pct: 40, line: "Captcha — your turn", label: "Open", action: "focus" },
);

check(
  "a terms wall says so, not 'captcha'",
  hdPillView({ today: TODAY, captchaWaiting: { kind: "terms" } }).line,
  "Terms page — your turn",
);

check(
  "stopped at the cap → capped, View opens History",
  pick(hdPillView({ today: TODAY, todayDate: TODAY, todayCount: 30, campaignCaps: { dailyTotal: 30 } })),
  { phase: "capped", done: 30, cap: 30, pct: 100, line: "Done for today", label: "View", action: "history" },
);

check(
  "stopped below the cap → Start goes to the dashboard (its launch gates), never starts here",
  pick(hdPillView({ today: TODAY, todayDate: TODAY, todayCount: 0, campaignCaps: { dailyTotal: 30 } })),
  { phase: "stopped", done: 0, cap: 30, pct: 0, line: "Not running", label: "Start", action: "dashboard" },
);

check(
  "admin 10M sentinel → no denominator, never 'capped', empty bar",
  pick(hdPillView({ today: TODAY, todayDate: TODAY, todayCount: 50, campaignCaps: { dailyTotal: 10_000_000 } })),
  { phase: "stopped", done: 50, cap: null, pct: 0, line: "Stopped", label: "Start", action: "dashboard" },
);

// --- where it refuses to show ------------------------------------------------------------

check("plain tab, no campaign → shows", hdPillHidden({ host: "www.indeed.com", tabId: 3, windowId: 1 }), false);
check(
  "the campaign's own tab → hidden",
  hdPillHidden({ host: "www.indeed.com", tabId: 3, windowId: 1, campaignRunning: true, campaignTabId: 3, campaignWindowId: 9 }),
  true,
);
check(
  "another tab in the campaign window → hidden (the walk roams tabs inside it)",
  hdPillHidden({ host: "boards.greenhouse.io", tabId: 4, windowId: 9, campaignRunning: true, campaignTabId: 3, campaignWindowId: 9 }),
  true,
);
check(
  "user's own tab while a campaign runs elsewhere → shows",
  hdPillHidden({ host: "www.indeed.com", tabId: 5, windowId: 1, campaignRunning: true, campaignTabId: 3, campaignWindowId: 9 }),
  false,
);
check(
  "stale campaign ids after a stop don't hide anything",
  hdPillHidden({ host: "www.indeed.com", tabId: 3, windowId: 9, campaignRunning: false, campaignTabId: 3, campaignWindowId: 9 }),
  false,
);
check(
  "unknown tab (PILL_TAB failed) with a campaign running → shows rather than guessing",
  hdPillHidden({ host: "www.indeed.com", tabId: null, windowId: null, campaignRunning: true, campaignTabId: null, campaignWindowId: null }),
  false,
);
check(
  "site hidden from the '−' chip → hidden",
  hdPillHidden({ host: "www.indeed.com", pillHiddenHosts: ["www.indeed.com"] }),
  true,
);

if (failures) {
  console.error(`\n${failures} failure(s)`);
  process.exit(1);
}
console.log("\nedge-pill: all checks passed");
