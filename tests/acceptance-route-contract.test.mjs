import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";

const require = createRequire(new URL("../governance/package.json", import.meta.url));
const ts = require("typescript-governance");
const source = readFileSync(new URL("../governance/apps/web/src/App.tsx", import.meta.url), "utf8");
const profiles = [
  "../acceptance/profile.json",
  "../governance/acceptance/profile.json",
];

// Read actual JSX route declarations. Comments, strings, wildcard fallbacks and
// parameter templates do not establish a concrete acceptance destination.
function concreteRoutes(text) {
  const file = ts.createSourceFile("App.tsx", text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  assert.equal(file.parseDiagnostics.length, 0, "The application route source must parse");
  const paths = new Set();
  function visit(node) {
    if ((ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node))
      && node.tagName.getText(file) === "Route") {
      for (const attribute of node.attributes.properties) {
        if (!ts.isJsxAttribute(attribute) || attribute.name.getText(file) !== "path") continue;
        const value = attribute.initializer;
        if (value && ts.isStringLiteral(value) && value.text.startsWith("/")
          && !value.text.includes("*") && !value.text.includes(":")) paths.add(value.text);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(file);
  return paths;
}

function validateDestinations(profile, routes) {
  const requested = profile?.surfaces?.pcRoutes;
  if (!Array.isArray(requested) || requested.length === 0) return ["pcRoutes must be a nonempty array"];
  const errors = [];
  const seen = new Set();
  for (const path of requested) {
    if (typeof path !== "string" || !routes.has(path)) errors.push(`Unregistered concrete PC route: ${String(path)}`);
    if (seen.has(path)) errors.push(`Duplicate PC route: ${String(path)}`);
    seen.add(path);
  }
  return errors;
}

const routes = concreteRoutes(source);
for (const path of profiles) {
  test(`every declared Tiger PC acceptance destination is an actual registered route: ${path}`, () => {
    const profile = JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
    assert.deepEqual(validateDestinations(profile, routes), []);
  });
}

test("root and governance acceptance destinations stay aligned", () => {
  const loaded = profiles.map((path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8")));
  assert.deepEqual(loaded[0].surfaces, loaded[1].surfaces);
});

test("the former /nights typo fails even though the application has a wildcard fallback", () => {
  assert.equal(routes.has("/night"), true);
  assert.equal(routes.has("/nights"), false);
  assert.deepEqual(validateDestinations({ surfaces: { pcRoutes: ["/nights"] } }, routes),
    ["Unregistered concrete PC route: /nights"]);
});

test("comments, text and dynamic route expressions cannot make an invalid page pass", () => {
  const parsed = concreteRoutes(`
    // <Route path="/comment" />
    const label = '<Route path="/string" />';
    const element = <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/night"><Night /></Route>
      <Route path="*" element={<NotFound />} />
      <Route path="/tasks/:threadId" element={<Task />} />
      <Route path={runtimeValue} element={<Unknown />} />
    </Routes>;
  `);
  assert.deepEqual([...parsed].sort(), ["/", "/night"]);
  for (const path of ["/comment", "/string", "/not-found", "/tasks/:threadId"]) {
    assert.equal(validateDestinations({ surfaces: { pcRoutes: [path] } }, parsed).length, 1);
  }
});

test("missing, empty, malformed and duplicate destinations fail instead of supplying defaults", () => {
  for (const profile of [{}, { surfaces: {} }, { surfaces: { pcRoutes: [] } },
    { surfaces: { pcRoutes: " /night " } }, { surfaces: { pcRoutes: [null] } },
    { surfaces: { pcRoutes: ["/night", "/night"] } }]) {
    assert.ok(validateDestinations(profile, routes).length > 0);
  }
});

test("invalid JSX is an error rather than a partial route inventory", () => {
  assert.throws(() => concreteRoutes('const broken = <Route path="/night"'), /must parse/);
});
