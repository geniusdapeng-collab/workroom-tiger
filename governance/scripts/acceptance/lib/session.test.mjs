import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { EXTERNAL_FONT_ROUTE, blockExternalFontCdn } from "./session.mjs";

test("acceptance blocks only external font CDN requests", async () => {
  let registered;
  const aborted = [];
  const page = {
    async route(pattern, handler) {
      registered = pattern;
      await handler({ abort: (reason) => aborted.push(reason) });
    },
  };
  await blockExternalFontCdn(page);
  assert.equal(registered, EXTERNAL_FONT_ROUTE);
  assert.equal(EXTERNAL_FONT_ROUTE.test("https://fonts.googleapis.com/css2?family=Orbitron"), true);
  assert.equal(EXTERNAL_FONT_ROUTE.test("https://fonts.gstatic.com/s/orbitron/font.woff2"), true);
  assert.equal(EXTERNAL_FONT_ROUTE.test("https://example.com/font.woff2"), false);
  assert.deepEqual(aborted, ["blockedbyclient"]);
});

test("web shell does not block startup on the external font stylesheet", () => {
  const html = readFileSync(new URL("../../../apps/web/index.html", import.meta.url), "utf8");
  assert.match(html, /rel="stylesheet"\s+media="print"\s+onload="this\.media='all'"/u);
  assert.match(html, /<noscript>[\s\S]*fonts\.googleapis\.com[\s\S]*<\/noscript>/u);
});
