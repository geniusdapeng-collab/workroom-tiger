import { afterEach, describe, expect, it, vi } from "vitest";
import { fixtureDatabaseUrls, governanceFixtureIdentity } from "./governance.js";

const valid = {
  DATABASE_URL: "postgres://postgres:mock@127.0.0.1:55432/workloom_tiger_fixture",
  DATABASE_APP_URL: "postgres://workloom_app:mock@127.0.0.1:55432/workloom_tiger_fixture",
  DATABASE_GATEWAY_URL: "postgres://workloom_gateway:mock@127.0.0.1:55432/workloom_tiger_fixture",
};

function configure(overrides: Partial<Record<keyof typeof valid, string | undefined>> = {}) {
  for (const [key, value] of Object.entries({ ...valid, ...overrides })) vi.stubEnv(key, value);
}

afterEach(() => vi.unstubAllEnvs());

describe("disposable governance fixture guard", () => {
  it("accepts three explicit roles in the same isolated PostgreSQL database", () => {
    configure();
    expect(fixtureDatabaseUrls()).toEqual({
      owner: valid.DATABASE_URL,
      app: valid.DATABASE_APP_URL,
      gateway: valid.DATABASE_GATEWAY_URL,
    });
  });

  it.each(Object.keys(valid) as Array<keyof typeof valid>)("rejects missing %s before opening any connection", (name) => {
    configure({ [name]: undefined });
    expect(() => fixtureDatabaseUrls()).toThrow("must explicitly identify a disposable test database");
  });

  it.each([
    ["remote host", "postgres://postgres:mock@db.example.com/workloom_tiger_fixture"],
    ["ordinary database", "postgres://postgres:mock@127.0.0.1:55432/workloom"],
    ["non-PostgreSQL URL", "https://postgres:mock@127.0.0.1:55432/workloom_tiger_fixture"],
  ])("rejects %s before fixture writes", (_reason, url) => {
    configure({ DATABASE_URL: url });
    expect(() => fixtureDatabaseUrls()).toThrow("must use loopback and a workloom_tiger_* test database");
  });

  it.each([
    ["other database", "postgres://workloom_app:mock@127.0.0.1:55432/workloom_tiger_other"],
    ["other port", "postgres://workloom_app:mock@127.0.0.1:55433/workloom_tiger_fixture"],
  ])("rejects a role pointing at %s", (_reason, url) => {
    configure({ DATABASE_APP_URL: url });
    expect(() => fixtureDatabaseUrls()).toThrow("All fixture database roles must target the same disposable database");
  });

  it.each(["DATABASE_APP_URL", "DATABASE_GATEWAY_URL"] as const)("rejects owner privilege substituted for %s", (name) => {
    configure({ [name]: valid.DATABASE_URL });
    expect(() => fixtureDatabaseUrls()).toThrow("Fixtures must exercise workloom_app and workloom_gateway role boundaries");
  });
});

describe("fixture scope identity", () => {
  it("keeps separate runs in separate tenants, workspaces, and skills", () => {
    const first = governanceFixtureIdentity("suite-a");
    const second = governanceFixtureIdentity("suite-b");
    for (const key of Object.keys(first) as Array<keyof typeof first>) expect(first[key]).not.toBe(second[key]);
  });

  it.each(["", "../other", "bad run", "bad'run"])("rejects an unsafe run identifier %j", (run) => {
    expect(() => governanceFixtureIdentity(run)).toThrow("Invalid fixture run identifier");
  });
});
