/**
 * service · 存储引导（AI 服务前台）
 * 表结构由 packages/db 迁移（并行底座代理）落地：c_users/c_conversations/c_messages/
 * c_notifications/c_tickets/c_ticket_events/kb_collections/kb_documents/kb_chunks/
 * kb_sources，全部带 RLS（app.workspace_id GUC）。历史示例表由显式迁移维护。
 * 本模块职责：
 *  - 只读检查通用迁移表是否可用；行业种子必须由显式装配流程写入
 *  - Markdown 切块 + 检索索引重建（kb_chunks）
 * 纪律：启动检查使用 app 池且不读写业务行；业务读写经 serviceTx/svcQuery。
 */
import { getAppPool } from "@workloom/db";

let bootstrapped: Promise<void> | null = null;

/** 幂等引导（每进程一次；失败置空允许下次调用重试，不永久卡死） */
export function ensureServiceSchema(): Promise<void> {
  if (!bootstrapped) {
    bootstrapped = bootstrap().catch((err) => {
      console.warn("[service-c] ensureServiceSchema 引导失败（允许重试）：", err instanceof Error ? err.message : err);
      bootstrapped = null;
      throw err;
    });
  }
  return bootstrapped;
}

interface SqlClient { query: (sql: string, params?: unknown[]) => Promise<{ rows: Array<Record<string, unknown>> }> }

async function bootstrap(): Promise<void> {
  const client = await getAppPool().connect();
  try {
    // 固定表名、零行业数据访问：缺表/权限失败立即拒绝，并允许迁移修复后重试。
    for (const table of [
      "c_users", "c_conversations", "c_messages", "c_notifications", "c_tickets", "c_ticket_events",
      "kb_collections", "kb_documents", "kb_chunks", "kb_sources",
    ]) await client.query(`SELECT 1 FROM ${table} WHERE false`);
  } finally {
    client.release();
  }
}

/** Markdown 切块：按二级标题分段（无标题则整体一块），供检索命中引用 */
export function splitChunks(md: string): Array<{ heading: string; content: string }> {
  const lines = md.split("\n");
  const chunks: Array<{ heading: string; content: string }> = [];
  let heading = "";
  let buf: string[] = [];
  const flush = () => {
    const content = buf.join("\n").trim();
    // 剔除纯标题空块（去掉 # 行后无正文），避免检索误命中空答案
    const body = content.replace(/^#+\s.*$/gm, "").trim();
    if (body) chunks.push({ heading, content: content.slice(0, 2000) });
    buf = [];
  };
  for (const line of lines) {
    if (line.startsWith("## ")) {
      flush();
      heading = line.replace(/^##\s+/, "").trim();
    } else if (line.startsWith("# ")) {
      flush();
      heading = "";
      buf.push(line);
    } else {
      buf.push(line);
    }
  }
  flush();
  return chunks;
}

/** 重建某文档的切块索引（同事务内调用；embedding/keywords 由底座向量化管线补，本层留空） */
export async function indexChunks(
  client: SqlClient,
  workspaceId: string,
  documentId: string,
  contentMd: string,
): Promise<number> {
  await client.query(`DELETE FROM kb_chunks WHERE workspace_id=$1 AND document_id=$2`, [workspaceId, documentId]);
  const chunks = splitChunks(contentMd);
  for (let i = 0; i < chunks.length; i++) {
    await client.query(
      `INSERT INTO kb_chunks (workspace_id, document_id, chunk_index, heading, content)
       VALUES ($1,$2,$3,$4,$5)`,
      [workspaceId, documentId, i, chunks[i]!.heading, chunks[i]!.content],
    );
  }
  return chunks.length;
}
