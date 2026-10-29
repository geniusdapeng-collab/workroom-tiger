import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const bridge = readFileSync(new URL("../../../apps/web/src/shell/Bridge.tsx", import.meta.url), "utf8");
const brake = readFileSync(new URL("../../../apps/web/src/components/hud/EmergencyBrake.tsx", import.meta.url), "utf8");

test("top-bar emergency brake invokes the real night-shift pause route", () => {
  assert.match(bridge, /trpc\.nightShift\.current\.query\(\)/u);
  assert.match(bridge, /navigate\("\/night"\)/u);
  assert.match(bridge, /trpc\.nightShift\.pause\.mutate\(\{ runId: current\.run\.id \}\)/u);
  assert.match(bridge, /finally\s*\{\s*setBrakeBusy\(false\);\s*\}/u);
  assert.match(bridge, /<EmergencyBrake onConfirm=\{\(\) => void pauseNightShift\(\)\} busy=\{brakeBusy\} \/>/u);
});

test("emergency brake retains the two-step confirm and withdraw interaction", () => {
  assert.match(brake, /onConfirm\?: \(\) => void/u);
  assert.match(brake, /disabled=\{busy \|\| unavailable\}/u);
  assert.match(brake, /确认制动（全端 ≤60s 生效）/u);
  assert.match(brake, /撤回/u);
  assert.match(brake, /onConfirm\?\.\(\); setArming\(false\)/u);
});
