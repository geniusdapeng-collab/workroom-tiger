import { describe, expect, it } from "vitest";
import { assertFrontendFixtureDatabase } from "./frontend-database.js";

const databaseKeys = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"] as const;
type Environment = Record<string, string | undefined>;

function fixture(port = "49153", host = "127.0.0.1"): Environment {
  return {
    TIGER_TEST_DATABASE_PORT: port,
    DATABASE_URL: `postgres://postgres:mock@${host}:${port}/workloom_tiger_fixture`,
    DATABASE_APP_URL: `postgres://workloom_app:mockapp@${host}:${port}/workloom_tiger_fixture`,
    DATABASE_GATEWAY_URL: `postgres://workloom_gateway:mockgw@${host}:${port}/workloom_tiger_fixture`,
  };
}

describe("frontend PostgreSQL fixture target guard", () => {
  it.each(["1", "5432", "49153", "55432", "65535"])("accepts the explicit service port %s", (port) => {
    expect(() => assertFrontendFixtureDatabase(fixture(port))).not.toThrow();
  });

  it("retains local port 55432 when the CI port variable is absent", () => {
    const env = fixture("55432");
    delete env.TIGER_TEST_DATABASE_PORT;
    expect(() => assertFrontendFixtureDatabase(env)).not.toThrow();
    expect(() => assertFrontendFixtureDatabase({ ...fixture(), TIGER_TEST_DATABASE_PORT: undefined }))
      .toThrow("expected loopback port");
  });

  it.each(["127.0.0.1", "[::1]"])("accepts the same literal loopback host %s for all roles", (host) => {
    const env = fixture("49153", host);
    env.DATABASE_APP_URL = env.DATABASE_APP_URL!.replace("postgres:", "postgresql:");
    expect(() => assertFrontendFixtureDatabase(env)).not.toThrow();
  });

  it.each([
    "", "0", "65536", "-1", "0001", "049153", "+49153", " 49153", "49153 ",
    "49153.0", "4.9153e4", "0xB001", "１２３", "9".repeat(200),
    "49153\nDATABASE_URL=injected", "49153; printf injected",
  ])("rejects invalid expected port %j before connection use", (port) => {
    expect(() => assertFrontendFixtureDatabase({ ...fixture(), TIGER_TEST_DATABASE_PORT: port }))
      .toThrow("canonical decimal port from 1 to 65535");
  });

  it.each(databaseKeys)("requires explicit %s", (key) => {
    for (const value of [undefined, "", " "]) {
      expect(() => assertFrontendFixtureDatabase({ ...fixture(), [key]: value })).toThrow();
    }
  });

  it.each(databaseKeys)("rejects remote, named or other loopback hosts in %s", (key) => {
    for (const host of ["db.example.com", "localhost", "127.0.0.2", "0.0.0.0", "127.0.0.1.example.com", "[::]"]) {
      const env = fixture();
      env[key] = env[key]!.replace("127.0.0.1", host);
      expect(() => assertFrontendFixtureDatabase(env)).toThrow("expected loopback port");
    }
  });

  it.each(databaseKeys)("rejects a different or absent port in %s", (key) => {
    for (const port of [":55432", ":49154", ""]) {
      const env = fixture();
      env[key] = env[key]!.replace(":49153", port);
      expect(() => assertFrontendFixtureDatabase(env)).toThrow("expected loopback port");
    }
  });

  it.each(databaseKeys)("rejects unsafe database names in %s", (key) => {
    for (const database of ["workloom", "workloom_tiger_", "workloom_tiger_UPPER", "workloom_tiger_fixture/other", "workloom_tiger_%66ixture"]) {
      const env = fixture();
      env[key] = env[key]!.replace("workloom_tiger_fixture", database);
      expect(() => assertFrontendFixtureDatabase(env)).toThrow("expected loopback port");
    }
  });

  it.each(["DATABASE_APP_URL", "DATABASE_GATEWAY_URL"] as const)("rejects a different database or host in %s", (key) => {
    const env = fixture();
    expect(() => assertFrontendFixtureDatabase({ ...env, [key]: env[key]!.replace("fixture", "other") }))
      .toThrow("same disposable database");
    expect(() => assertFrontendFixtureDatabase({ ...env, [key]: env[key]!.replace("127.0.0.1", "[::1]") }))
      .toThrow("same disposable database");
  });

  it.each(databaseKeys)("rejects non-PostgreSQL and malformed %s", (key) => {
    for (const raw of ["not a URL", "//127.0.0.1:49153/workloom_tiger_fixture", fixture()[key]!.replace("postgres:", "https:"), fixture()[key]!.replace("postgres:", "mysql:")]) {
      expect(() => assertFrontendFixtureDatabase({ ...fixture(), [key]: raw })).toThrow();
    }
  });

  it.each(databaseKeys)("rejects connection overrides, fragments and control characters in %s", (key) => {
    const env = fixture();
    for (const raw of [
      `${env[key]}?host=db.example.com`, `${env[key]}?user=postgres&dbname=workloom`,
      `${env[key]}#ignored`, ` ${env[key]}`, `${env[key]}\n`,
      env[key]!.replace("49153", "491\t53"),
    ]) {
      expect(() => assertFrontendFixtureDatabase({ ...env, [key]: raw })).toThrow("expected loopback port");
    }
  });

  it.each([
    ["DATABASE_APP_URL", "postgres"], ["DATABASE_APP_URL", "workloom_gateway"],
    ["DATABASE_APP_URL", "workloom%5fapp"], ["DATABASE_GATEWAY_URL", "postgres"],
    ["DATABASE_GATEWAY_URL", "workloom_app"], ["DATABASE_URL", "workloom_app"],
    ["DATABASE_URL", "workloom_gateway"], ["DATABASE_URL", ""],
    ["DATABASE_URL", "workloom%5fapp"], ["DATABASE_URL", "%77orkloom_app"],
    ["DATABASE_URL", "workloom%5fgateway"], ["DATABASE_URL", "postgres%00"],
  ] as const)("rejects substituted role %s=%s", (key, role) => {
    const env = fixture();
    const url = new URL(env[key]!);
    url.username = role;
    env[key] = url.toString();
    expect(() => assertFrontendFixtureDatabase(env)).toThrow("preserve distinct owner");
  });

  it("does not mutate the supplied environment or disclose connection credentials in failures", () => {
    const env = Object.freeze(fixture());
    const before = { ...env };
    assertFrontendFixtureDatabase(env);
    expect(env).toEqual(before);
    const unsafe = { ...env, DATABASE_URL: "postgres://postgres:fixture-only-password@db.example.com:49153/workloom_tiger_fixture" };
    let message = "";
    try { assertFrontendFixtureDatabase(unsafe); } catch (error) { message = (error as Error).message; }
    expect(message).toContain("DATABASE_URL");
    expect(message).not.toContain("fixture-only-password");
    expect(message).not.toContain("db.example.com");
  });
});
