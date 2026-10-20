/** Explicit C-service data for isolated PostgreSQL tests; never seeds product examples. */
import pg from "pg";
import { createHash } from "node:crypto";
import {
  createGovernanceFixture, governanceFixtureIdentity, fixtureDatabaseUrls,
} from "../test-fixtures/governance.js";
import { indexChunks } from "./store.js";

export function serviceFixtureIdentity(run: string) {
  const identity = governanceFixtureIdentity(run);
  return { ...identity, collectionId: `kbc-${identity.workspaceId}`, collectionName: `测试知识库 ${run}` };
}

export async function createServiceFixture(fixture: ReturnType<typeof serviceFixtureIdentity>): Promise<void> {
  await createGovernanceFixture(fixture);
  const db = new pg.Client({ connectionString: fixtureDatabaseUrls().owner });
  await db.connect();
  try {
    const content = [
      "# 隔离测试知识库",
      "",
      "## 决策日报几点发",
      "测试决策日报在盘前生成，所有输出均为模拟结果。",
      "",
      "## 系统什么情况下会开仓",
      "测试说明：系统须满足 MRS 风控条件才会开仓，此段仅用于知识检索测试。",
      "",
      "## 开仓的硬逻辑是什么",
      "测试开仓的硬逻辑以 MRS 风控结果为依据，此段仅用于知识检索测试。",
      "",
      "## Wi-Fi 密码",
      "测试网络的 Wi-Fi 密码由管理员单独提供。",
    ].join("\n");
    const documentId = `kbd-${fixture.workspaceId}`;
    await db.query("BEGIN");
    await db.query("INSERT INTO kb_collections (id,workspace_id,name,description) VALUES ($1,$2,$3,'隔离测试资料')",
      [fixture.collectionId, fixture.workspaceId, fixture.collectionName]);
    await db.query(
      `INSERT INTO kb_documents (id,workspace_id,collection_id,title,source_kind,content_md,hash,status)
       VALUES ($1,$2,$3,'隔离测试知识','manual',$4,$5,'active')`,
      [documentId, fixture.workspaceId, fixture.collectionId, content, createHash("sha256").update(content).digest("hex")],
    );
    await indexChunks(db, fixture.workspaceId, documentId, content);
    await db.query("COMMIT");
  } catch (error) {
    await db.query("ROLLBACK");
    throw error;
  } finally {
    await db.end();
  }
}
