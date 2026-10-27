/**
 * The frontend PostgreSQL suite must validate its disposable target before opening pools.
 * CI passes the allocated service port explicitly; local runs retain port 55432 by default.
 * This test-only guard has no database, model, or production-module dependencies.
 */
export function assertFrontendFixtureDatabase(
  env: Readonly<Record<string, string | undefined>> = process.env,
): void {
  const expectedPort = env.TIGER_TEST_DATABASE_PORT ?? "55432";
  if (!/^[1-9][0-9]{0,4}$/.test(expectedPort) || Number(expectedPort) > 65535) {
    throw new Error("TIGER_TEST_DATABASE_PORT must be a canonical decimal port from 1 to 65535");
  }

  const names = ["DATABASE_URL", "DATABASE_APP_URL", "DATABASE_GATEWAY_URL"] as const;
  const urls = names.map((name) => {
    const raw = env[name];
    if (!raw) throw new Error(`${name} must explicitly identify a disposable test database`);
    let url: URL;
    try {
      url = new URL(raw);
    } catch {
      throw new Error(`${name} must be a valid isolated PostgreSQL URL`);
    }
    // Query parameters can override pg connection fields; the checked URL must be the target.
    if (/[\u0000-\u0020\u007f]/.test(raw) || url.search || url.hash
      || !["postgres:", "postgresql:"].includes(url.protocol)
      || !["127.0.0.1", "[::1]"].includes(url.hostname)
      || url.port !== expectedPort || !/^\/workloom_tiger_[a-z0-9_]+$/.test(url.pathname)) {
      throw new Error(`${name} must use the expected loopback port and a workloom_tiger_* test database`);
    }
    return url;
  });
  const [owner, app, gateway] = urls;
  if (!owner || !app || !gateway
    || urls.some((url) => url.hostname !== owner.hostname || url.pathname !== owner.pathname)) {
    throw new Error("All frontend fixture database roles must target the same disposable database");
  }
  if (!/^[a-z_][a-z0-9_]*$/i.test(owner.username) || ["workloom_app", "workloom_gateway"].includes(owner.username)
    || app.username !== "workloom_app" || gateway.username !== "workloom_gateway") {
    throw new Error("Frontend fixtures must preserve distinct owner, workloom_app and workloom_gateway roles");
  }
}
