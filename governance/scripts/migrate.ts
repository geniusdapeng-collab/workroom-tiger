/**
 * 迁移执行器（手写 SQL + tsx；drizzle schema 仅作类型源）
 * 用法：pnpm db:migrate（读取 .env）
 * 步骤：① 确保应用双角色存在（密码取自环境变量，不硬编码入库）
 *      ② 建 _migrations 账本；③ 按文件名顺序执行未应用迁移（单事务）
 */
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import pg from "pg";

const __dirname = dirname(fileURLToPath(import.meta.url));
const MIGRATIONS_DIR = join(__dirname, "../packages/db/migrations");

const DATABASE_URL = process.env.DATABASE_URL ?? "postgres://postgres:workloom@localhost:5432/workloom";
const APP_DB_PASSWORD = process.env.APP_DB_PASSWORD ?? "workloom_dev_app";
const GATEWAY_DB_PASSWORD = process.env.GATEWAY_DB_PASSWORD ?? "workloom_dev_gateway";

function sqlEscape(s: string): string {
  return s.replace(/'/g, "''");
}

async function ensureRoles(client: pg.Client): Promise<void> {
  const roles: Array<[string, string]> = [
    ["workloom_app", APP_DB_PASSWORD],
    ["workloom_gateway", GATEWAY_DB_PASSWORD],
  ];
  for (const [role, pwd] of roles) {
    await client.query(
      `DO $$ BEGIN
         IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${role}') THEN
           CREATE ROLE ${role} LOGIN PASSWORD '${sqlEscape(pwd)}';
         END IF;
       END $$;`,
    );
    console.log(`✓ 角色就绪: ${role}`);
  }
}

async function main(): Promise<void> {
  const client = new pg.Client({ connectionString: DATABASE_URL });
  await client.connect();
  try {
    await ensureRoles(client);
    await client.query(
      "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    );
    const applied = new Set(
      (await client.query("SELECT name FROM _migrations")).rows.map((r) => r.name as string),
    );
    const files = readdirSync(MIGRATIONS_DIR)
      .filter((f) => f.endsWith(".sql"))
      .sort();
    let count = 0;
    for (const file of files) {
      if (applied.has(file)) {
        console.log(`– 跳过（已应用）: ${file}`);
        continue;
      }
      const sql = readFileSync(join(MIGRATIONS_DIR, file), "utf-8");
      await client.query("BEGIN");
      try {
        await client.query(sql);
        await client.query("INSERT INTO _migrations (name) VALUES ($1)", [file]);
        await client.query("COMMIT");
        count += 1;
        console.log(`✓ 已应用: ${file}`);
      } catch (err) {
        await client.query("ROLLBACK");
        throw err;
      }
    }
    console.log(count === 0 ? "数据库已是最新。" : `迁移完成，共应用 ${count} 个文件。`);
  } finally {
    await client.end();
  }
}

main().catch((err) => {
  console.error("迁移失败：", err?.message ?? err);
  process.exit(1);
});
