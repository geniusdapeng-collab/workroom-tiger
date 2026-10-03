import { createHash } from "node:crypto";
import { lstatSync, readFileSync, realpathSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve } from "node:path";

export const PROPOSAL_KIND = "wfa.param.proposal";
export const APPROVAL_SCHEMA = "tiger.wfa-approval/v1";
export const TIGER_SCOPE = Object.freeze({ tenantId: "tiger", workspaceId: "trading" });
export function sha256(value) { return createHash("sha256").update(value).digest("hex"); }
export function canonical(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
}

function ordinaryPath(file, directory = false) {
  const stat = lstatSync(file);
  if (stat.isSymbolicLink() || (directory ? !stat.isDirectory() : !stat.isFile())) throw new Error("审批文件与目录必须为普通文件/目录，禁止符号链接");
  return realpathSync(file);
}

export function reviewContext({ kernelRoot, pythonExe, proposalsDir, environment = "paper" }) {
  if (environment !== "paper") throw new Error("审批桥只允许 paper 环境");
  if (![kernelRoot, pythonExe, proposalsDir].every((item) => typeof item === "string" && isAbsolute(item))) throw new Error("必须提供绝对 TIGER_KERNEL_ROOT、TIGER_PYTHON_EXE 与提案目录");
  const kernel = ordinaryPath(kernelRoot, true);
  const directory = ordinaryPath(proposalsDir, true);
  if (basename(directory) !== "review_proposals") throw new Error("提案目录必须为独立 review_proposals 目录");
  if (directory === kernel || directory.startsWith(`${kernel}/`) || directory.startsWith(`${kernel}\\`)) throw new Error("业务提案不得写在内核代码目录中");
  const python = ordinaryPath(realpathSync(pythonExe));
  ordinaryPath(join(kernel, "main.py"));
  const configSha256 = sha256(readFileSync(ordinaryPath(join(kernel, "trading_system", "config.py"))));
  return Object.freeze({ kernelRoot: kernel, pythonExe: python, proposalsDir: directory, outDir: dirname(directory), environment, configSha256 });
}

export function readProposal(context, id) {
  if (typeof id !== "string" || !/^PROP-[A-Za-z0-9_-]{1,120}$/u.test(id)) throw new Error("非法提案编号");
  const file = ordinaryPath(join(context.proposalsDir, `${id}.json`));
  if (lstatSync(file).size > 2_000_000) throw new Error("审批提案文件超出大小上限");
  if (dirname(file) !== context.proposalsDir) throw new Error("提案文件逃出受控目录");
  const raw = readFileSync(file);
  const proposal = JSON.parse(raw.toString("utf8"));
  if (proposal.proposal_id !== id) throw new Error("文件名与提案编号不一致");
  const params = proposal.grid_result?.recommended_params;
  if (!params || typeof params !== "object" || Array.isArray(params) || !Object.keys(params).length) throw new Error("提案无推荐参数");
  return { file, proposal, sha256: sha256(raw), parametersSha256: sha256(canonical(params)) };
}

export function approvalSnapshot(context, id, now = Date.now()) {
  const record = readProposal(context, id);
  if (record.proposal.status !== "pending_review" || record.proposal.verdict !== "pending_review") throw new Error("只有待审 WFA 提案可以进入审批");
  if (!Number.isFinite(record.proposal.dsr) || !Number.isFinite(record.proposal.oos_expectancy)) throw new Error("提案统计值必须为有限数值");
  return {
    schemaVersion: APPROVAL_SCHEMA, kind: PROPOSAL_KIND,
    tenant_id: TIGER_SCOPE.tenantId, workspace_id: TIGER_SCOPE.workspaceId,
    environment: context.environment, proposal_id: id,
    proposal_sha256: record.sha256, parameters_sha256: record.parametersSha256,
    config_sha256: context.configSha256, proposals_dir: context.proposalsDir,
    created_at: new Date(now).toISOString(), expires_at: new Date(now + 24 * 60 * 60 * 1000).toISOString(),
    high_risk: true, action: "param.change", gestures: ["approve", "reject"],
    dsr: record.proposal.dsr, oos_expectancy: record.proposal.oos_expectancy,
    recommended_params: record.proposal.grid_result.recommended_params,
    before: null, after: record.proposal.grid_result.recommended_params,
    edit_policy: "编辑参数须生成新提案重新审批",
  };
}

export function executionIdentity(row) {
  return `tiger-approval-${sha256(canonical({ approval_id: row.approval_id, snapshot: row.snapshot, status: row.status, gesture_type: row.gesture?.type, reason_enum: row.gesture?.reason_enum ?? "", reason_text: row.gesture?.reason_text ?? "", decided_by: row.decided_by, decided_at: new Date(row.decided_at).toISOString() }))}`;
}

export function validateApproval(row, context, now = Date.now()) {
  const snapshot = row.snapshot;
  if (row.tenant_id !== TIGER_SCOPE.tenantId || row.workspace_id !== TIGER_SCOPE.workspaceId
      || snapshot?.tenant_id !== TIGER_SCOPE.tenantId || snapshot?.workspace_id !== TIGER_SCOPE.workspaceId) throw new Error("审批租户或工作区不匹配");
  if (snapshot?.schemaVersion !== APPROVAL_SCHEMA || snapshot?.kind !== PROPOSAL_KIND || snapshot?.environment !== "paper") throw new Error("审批类型或环境不匹配");
  if (![snapshot.proposal_sha256, snapshot.parameters_sha256].every((hash) => typeof hash === "string" && /^[a-f0-9]{64}$/u.test(hash)) || !snapshot.recommended_params) throw new Error("审批缺少可信提案/参数摘要");
  if (snapshot.proposal_id !== row.approval_id || snapshot.proposals_dir !== context.proposalsDir || snapshot.config_sha256 !== context.configSha256) throw new Error("审批目录、配置或编号已变化");
  if (row.gesture?.executed === true || row.gesture?.execution_receipt) throw new Error("审批已消费，禁止重放");
  if (row.status !== "approved" && row.status !== "rejected") throw new Error("审批未裁决或编辑后未重新审批");
  if (row.gesture?.type !== (row.status === "approved" ? "approve" : "reject") || row.gesture?.edited_after !== undefined) throw new Error("裁决手势不匹配，编辑参数需重新提案");
  if (typeof row.decided_by !== "string" || !row.decided_by.trim()) throw new Error("审批缺少真实裁决人");
  const created = Date.parse(snapshot.created_at);
  const expiry = Date.parse(snapshot.expires_at);
  const decided = new Date(row.decided_at).getTime();
  if (![created, expiry, decided].every(Number.isFinite) || expiry <= created || expiry - created > 7 * 86_400_000 || decided < created || decided > expiry || decided > now + 60_000) throw new Error("审批有效期或裁决时间无效");
  let reason = "";
  if (row.status === "rejected") {
    if (typeof row.gesture.reason_enum !== "string" || !row.gesture.reason_enum.trim()) throw new Error("驳回必须附原因枚举");
    const text = row.gesture.reason_text ?? "";
    if (typeof text !== "string" || text.length > 200) throw new Error("驳回说明无效");
    reason = `${row.gesture.reason_enum.trim()}${text.trim() ? `：${text.trim()}` : ""}`;
  }
  const executionId = executionIdentity(row);
  const record = readProposal(context, row.approval_id);
  if (record.parametersSha256 !== snapshot.parameters_sha256
      || sha256(canonical(snapshot.recommended_params)) !== snapshot.parameters_sha256) throw new Error("审批参数摘要不匹配");
  const recovered = record.proposal.status === row.status
    && record.proposal.execution_id === executionId
    && record.proposal.preimage_sha256 === snapshot.proposal_sha256;
  if (!recovered && (expiry < now || record.sha256 !== snapshot.proposal_sha256 || record.proposal.status !== "pending_review")) throw new Error("审批已过期、提案已变化或已消费");
  return { ...record, reason, executionId, recovered };
}

export function verifyExecution(row, context, expected) {
  const record = readProposal(context, row.approval_id);
  if (record.proposal.status !== row.status || record.proposal.execution_id !== expected.executionId
      || record.proposal.preimage_sha256 !== row.snapshot.proposal_sha256
      || record.parametersSha256 !== row.snapshot.parameters_sha256) throw new Error("内核执行回执与受审提案不一致");
  let tuned = null;
  const receiptFile = ordinaryPath(join(context.outDir, "review_executions", `${expected.executionId}.json`));
  const receiptBytes = readFileSync(receiptFile);
  const kernelReceipt = JSON.parse(receiptBytes.toString("utf8"));
  if (kernelReceipt.proposal_id !== row.approval_id || kernelReceipt.execution_id !== expected.executionId
      || kernelReceipt.preimage_sha256 !== row.snapshot.proposal_sha256 || kernelReceipt.status !== row.status) throw new Error("不可变内核回执不匹配");
  if (row.status === "approved") {
    const effectPath = resolve(context.outDir, "review_executions", `${expected.executionId}.effect.json`);
    if (kernelReceipt.tuned_path !== effectPath || !/^[a-f0-9]{64}$/u.test(kernelReceipt.tuned_sha256 ?? "")) throw new Error("不可变参数回执路径或摘要无效");
    const tunedFile = ordinaryPath(effectPath);
    const raw = readFileSync(tunedFile);
    if (sha256(raw) !== kernelReceipt.tuned_sha256) throw new Error("不可变生效参数字节摘要不匹配");
    const value = JSON.parse(raw.toString("utf8"));
    if (value.proposal_id !== row.approval_id || value.execution_id !== expected.executionId
        || sha256(canonical(value.params)) !== row.snapshot.parameters_sha256
        || value.effective_from !== record.proposal.effective_from || value.effective_from !== kernelReceipt.effective_from || !/^\d{4}-\d{2}-\d{2}$/u.test(value.effective_from ?? "")) throw new Error("生效参数回执无效");
    tuned = { path: tunedFile, sha256: sha256(raw), effective_from: value.effective_from };
  } else if (record.proposal.reason !== expected.reason || kernelReceipt.reason !== expected.reason || kernelReceipt.tuned_path !== null || kernelReceipt.tuned_sha256 !== null) throw new Error("内核驳回原因不一致");
  return { schemaVersion: "tiger.approval-receipt/v1", execution_id: expected.executionId, proposal_id: row.approval_id,
    environment: "paper", proposal_sha256: record.sha256, preimage_sha256: row.snapshot.proposal_sha256,
    parameters_sha256: record.parametersSha256, decided_by: row.decided_by, tuned,
    kernel_receipt: { path: receiptFile, sha256: sha256(receiptBytes) },
    recovered: expected.recovered, executed_at: new Date().toISOString() };
}
