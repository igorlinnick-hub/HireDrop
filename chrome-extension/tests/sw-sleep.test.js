// Fixture test for the SW-raced sleep() in ../content.js — the engine clock that makes
// pauses immune to Chrome's background-window timer throttling. Same manual run as the
// other tests here:
//
//   mkdir -p /tmp/hd-gate && cd /tmp/hd-gate && npm i jsdom   # jsdom not actually needed here
//   node <repo>/jobflow/chrome-extension/tests/sw-sleep.test.js
//
// What must hold:
//   1. SW answers on time while the local clock is throttled → sleep ends on the SW clock.
//   2. SW never answers → the local timer still resolves (degrades to pre-fix behavior).
//   3. SW dies mid-wait (callback fires EARLY with no response) → the wait is NOT cut
//      short. This is the case that turns a flapping service worker into a page-hammering
//      zero-delay loop if it regresses.
//   4. Orphaned context (sendMessage throws synchronously) → local timer stands alone.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = fs.readFileSync(path.join(__dirname, "..", "content.js"), "utf8");
const START = SRC.indexOf("  // The engine's clock.");
const END = SRC.indexOf("  function rand(");
if (START < 0 || END < 0) {
  console.error("Could not locate sleep() in content.js — markers moved.");
  process.exit(2);
}
const SLEEP_SRC = SRC.slice(START, END);

function makeSandbox(swBehavior, localScale) {
  // localScale simulates window-timer throttling: the local setTimeout takes
  // localScale × the requested delay. The SW path uses the honest delay.
  const sandbox = {
    setTimeout: (fn, ms) => setTimeout(fn, ms * (localScale || 1)),
    chrome: {
      runtime: {
        lastError: null,
        sendMessage: (msg, cb) => {
          if (swBehavior === "throws") throw new Error("Extension context invalidated");
          if (swBehavior === "silent") return; // callback never fires
          if (swBehavior === "dead") { cb(undefined); return; } // early, no response
          setTimeout(() => cb({ ok: true }), msg.ms); // honest SW clock
        },
      },
    },
    result: null,
  };
  vm.createContext(sandbox);
  vm.runInContext(SLEEP_SRC + "\nresult = sleep;", sandbox);
  return sandbox;
}

async function timed(fn) {
  const t0 = Date.now();
  await fn();
  return Date.now() - t0;
}

(async () => {
  let failures = 0;
  const check = (name, ok, detail) => {
    if (!ok) failures++;
    console.log(`${ok ? "PASS" : "FAIL"}  ${name}  (${detail})`);
  };

  // 1. Throttled window (local clock 10× slow), live SW → resolves on the SW clock.
  {
    const sb = makeSandbox("ok", 10);
    const took = await timed(() => sb.result(200));
    check("throttled window, live SW → SW clock wins", took < 1000, `${took}ms for a 200ms sleep, local clock would take 2000ms`);
  }
  // 2. SW silent → local timer resolves at its own pace (here unthrottled).
  {
    const sb = makeSandbox("silent", 1);
    const took = await timed(() => sb.result(200));
    check("silent SW → local timer resolves", took >= 190 && took < 1000, `${took}ms`);
  }
  // 3. Dying SW fires the callback EARLY with no response → wait must NOT be cut short.
  {
    const sb = makeSandbox("dead", 1);
    const took = await timed(() => sb.result(300));
    check("dead SW early-callback must not shorten the wait", took >= 290, `${took}ms for a 300ms sleep`);
  }
  // 4. Orphaned context (sendMessage throws) → local timer stands alone, no unhandled throw.
  {
    const sb = makeSandbox("throws", 1);
    const took = await timed(() => sb.result(200));
    check("orphaned context → local timer, no throw", took >= 190, `${took}ms`);
  }

  console.log(`\n${4 - failures}/4 passed`);
  process.exit(failures ? 1 : 0);
})();
