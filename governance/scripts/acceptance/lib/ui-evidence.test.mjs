import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";

const HERE = dirname(fileURLToPath(import.meta.url));
const ACCEPTANCE = resolve(HERE, "..");
const SOURCE = process.env.MC031_SOURCE_DIR ? resolve(process.env.MC031_SOURCE_DIR) : ACCEPTANCE;
const GOVERNANCE = resolve(ACCEPTANCE, "../..");
const thresholds = {
  firstValueMs: 20000, approvalMs: 30000, dispatchMs: 60000,
  traceClicks: 2, guestFirstReplyMs: 30000, decisionQuota: 7,
};
const helper = () => import("./ui-evidence.mjs");
const hash = (value) => createHash("sha256").update(value).digest("hex");
const clone = (value) => JSON.parse(JSON.stringify(value));

const good = {
  "EXP-01": { healthy: true, valueVisible: true, ceremony: true, skipSucceeded: true, firstValueMs: 1000, decisionCount: 2, dailyCountComplete: true },
  "EXP-02": { healthy: true, gestures: { advance: true, calibrate: true, brake: true }, selectedApprovalId: "a-1", clicked: true, requestApprovalId: "a-1", receiptApprovalId: "a-1", gestureEventId: "e-2", approvalStatus: "approved", receiptVisible: true, readbackStatus: "approved", elapsedMs: 500 },
  "EXP-03": { healthy: true, eventId: "e-1", threadId: "T-1", clicked: true, clicks: 1, destinationThreadId: "T-1", destinationHasEvent: true, cardActor: true, cardEventVisible: true },
  "EXP-04": { healthy: true, entryEnabled: true, opened: true, scopeVisible: true, cancelled: true, closed: true, pauseRequests: 0 },
  "EXP-05": { healthy: true, inputVisible: true, filled: true, clicked: true, requestTitle: "本周复盘", title: "本周复盘", threadId: "T-2", responseOk: true, detailThreadId: "T-2", detailTitle: "本周复盘", planSteps: 2, planVisible: true, steps: 3, dispatchMs: 500 },
  "EXP-06": { healthy: true, titleVisible: true, sourceOk: true, expectedGroups: [{ workspaceId: "w-1", workspaceName: "模拟工作区", pendingApprovals: 2 }], renderedGroups: [{ workspaceName: "模拟工作区", pendingApprovals: 2 }], totalVisible: 2, emptyNextStep: false },
  "EXP-07": { healthy: true, member: true, workspaceMatches: true, titleVisible: true, sourceOk: true, expectedTasks: [{ id: "T-1", title: "复盘任务" }], renderedTasks: [{ id: "T-1", title: "复盘任务" }], overflow: false },
  "EXP-08": { healthy: true, titleVisible: true, sourceOk: true, expectedGrants: [{ id: "g-1", name: "本地伙伴", workspaces: "模拟工作区", capabilities: "经营报表", expires: "2027/1/1", revoked: false }], renderedGrants: [{ name: "本地伙伴", workspaces: "模拟工作区", capabilities: "经营报表", expires: "2027/1/1", revoked: false }], moneyRedline: true },
  "EXP-09": { healthy: true, inputVisible: true, clicked: true, question: "能提供发票吗？", requestText: "能提供发票吗？", requestOk: true, responseText: "请提供抬头，我可以协助核对。", conversationId: "conversation-1", assistantBefore: 1, assistantAfter: 2, userBefore: 0, userAfter: 1, newUserText: "能提供发票吗？", newAssistantText: "请提供抬头，我可以协助核对。", newAssistantComplete: true, assistantAfterUser: true, firstReplyMs: 100, replyHasHandoff: true, ticketsEntry: true, replyMock: true, demoLabeled: true },
  "EXP-10": { healthy: true, editorVisible: true, draftClicked: true, draftVisible: true, clicked: true, responseOk: true, dryRunId: "dry-1", reportRuleId: "R2", requestedRuleId: "R2", reportVisible: true, reportAbsentBefore: true, reportTextMatches: true, beforeRulesDigest: "rules", afterRulesDigest: "rules", beforeVersionsDigest: "versions", afterVersionsDigest: "versions", beforeApprovalsDigest: "approvals", afterApprovalsDigest: "approvals", confirmRequests: 0 },
};

test("profile identity accepts the rendered employee heading and preserves all other checks", async () => {
  const { profileEvidence } = await helper();
  const expected = { name: "测试岗位", versionLabel: "第 1 版", expectedFences: 1, expectedSkills: 1 };
  const dom = { heading: "员工档案 · 测试岗位", identity: "身份与归属 第 1 版 来源行业包", declared: 1, equipped: 1, dangling: false, uninstalled: false, notFound: false };
  assert.equal(profileEvidence(dom, expected).ok, true);
  assert.equal(profileEvidence({ ...dom, heading: "成员档案 · 测试岗位" }, expected).ok, true);
  for (const patch of [
    { heading: "员工档案 · " }, { heading: "员工档案 · 其他岗位" },
    { identity: "第 2 版 来源行业包" }, { identity: "第 1 版" },
    { declared: 0 }, { equipped: 0 }, { dangling: true }, { uninstalled: true }, { notFound: true },
  ]) assert.equal(profileEvidence({ ...dom, ...patch }, expected).ok, false, JSON.stringify(patch));
});

test("tRPC exchange parsing binds batch index, request body and response error", async () => {
  const { rpcExchange } = await helper();
  const url = "http://127.0.0.1/trpc/threads.list,threads.dispatch?batch=1";
  const result = [{ result: { data: [] } }, { result: { data: { threadId: "T-1" } } }];
  const parsed = rpcExchange(url, "POST", { 0: {}, 1: { title: "目标" } }, 200, result, "threads.dispatch");
  assert.deepEqual(parsed, { procedure: "threads.dispatch", input: { title: "目标" }, data: { threadId: "T-1" }, status: 200, ok: true });
  assert.equal(rpcExchange(url, "POST", { 1: { title: "目标" } }, 200, [{ result: { data: [] } }, { error: { message: "denied" } }], "threads.dispatch").ok, false);
  assert.equal(rpcExchange(url, "POST", {}, 503, result, "threads.dispatch").ok, false);
  assert.equal(rpcExchange(url, "POST", {}, 200, result, "approvals.decide"), null);
});

test("daily quota uses unique timestamped decision objects and detects truncated lists", async () => {
  const { dailyDecisionEvidence } = await helper();
  const at = new Date("2026-10-20T12:00:00Z");
  const rows = [
    { event_id: "e-1", created_at: "2026-10-20T01:00:00Z" },
    { event_id: "e-1", created_at: "2026-10-20T01:00:00Z" },
    { event_id: "e-2", created_at: "2026-10-19T01:00:00Z" },
  ];
  assert.equal(dailyDecisionEvidence(rows, at).count, 1);
  assert.equal(dailyDecisionEvidence(rows, at).complete, true);
  const fullToday = Array.from({ length: 50 }, (_, i) => ({ event_id: `e-${i}`, created_at: at.toISOString() }));
  assert.equal(dailyDecisionEvidence(fullToday, at).complete, false);
  assert.equal(dailyDecisionEvidence([{ event_id: "e-1", created_at: "invalid" }], at).complete, false);
  assert.equal(dailyDecisionEvidence(null, at).complete, false);
});

test("canonical snapshots ignore object key order but preserve every array order", async () => {
  const { canonicalJson } = await helper();
  assert.equal(canonicalJson({ b: 2, a: 1 }), canonicalJson({ a: 1, b: 2 }));
  assert.notEqual(canonicalJson({ rule: { actions: ["first", "second"] } }), canonicalJson({ rule: { actions: ["second", "first"] } }));
  assert.notEqual(canonicalJson([{ sequence: 1 }, { sequence: 2 }]), canonicalJson([{ sequence: 2 }, { sequence: 1 }]));
});

test("rendered identifiers reject generic labels and overlapping numeric suffixes", async () => {
  const { visibleIdentifier } = await helper();
  assert.equal(visibleIdentifier("账本事件 event-1", "event-1"), true);
  assert.equal(visibleIdentifier("账本事件 ···123", "event-123"), true);
  assert.equal(visibleIdentifier("账本事件 event-12", "event-1"), false);
  assert.equal(visibleIdentifier("账本事件 ···1234", "event-123"), false);
  assert.equal(visibleIdentifier("账本事件 编号已记录", "event-abc"), false);
  assert.equal(visibleIdentifier("账本事件 ···123", null), false);
});

for (const [id, valid] of Object.entries(good)) {
  test(`${id}: complete, bound evidence passes; absence and unhealthy pages do not`, async () => {
    const { evaluateJourney } = await helper();
    assert.equal(evaluateJourney(id, clone(valid), thresholds).pass, true);
    const missing = evaluateJourney(id, {}, thresholds);
    assert.equal(missing.pass, false);
    assert.equal(missing.status, "unverified");
    assert.ok(missing.missing.length > 0);
    assert.equal(evaluateJourney(id, { ...valid, healthy: false }, thresholds).pass, false);
  });
}

const falseGreens = [
  ["EXP-01", { skipSucceeded: false }, "failed welcome skip"],
  ["EXP-01", { decisionCount: 8 }, "quota is seven objects, not twenty-one keyword hits"],
  ["EXP-01", { dailyCountComplete: false }, "truncated daily list"],
  ["EXP-01", { firstValueMs: 20001 }, "first-value threshold"],
  ["EXP-02", { clicked: false }, "failed click"],
  ["EXP-02", { receiptApprovalId: "other" }, "receipt for a different approval"],
  ["EXP-02", { gestureEventId: null }, "static ledger wording without event id"],
  ["EXP-02", { readbackStatus: "pending" }, "write not persisted"],
  ["EXP-02", { elapsedMs: 30001 }, "approval threshold"],
  ["EXP-03", { destinationThreadId: "other" }, "wrong linked task"],
  ["EXP-03", { destinationHasEvent: false }, "missing original event"],
  ["EXP-03", { clicks: 3 }, "click budget"],
  ["EXP-04", { closed: false }, "withdraw button did not dismiss confirmation"],
  ["EXP-04", { pauseRequests: 1 }, "withdraw accidentally sent pause"],
  ["EXP-05", { inputVisible: false }, "API-only dispatch"],
  ["EXP-05", { clicked: false }, "dispatch click never succeeded"],
  ["EXP-05", { requestTitle: "other" }, "another request"],
  ["EXP-05", { detailThreadId: "other" }, "another task detail"],
  ["EXP-05", { planSteps: 0 }, "navigation words without a plan"],
  ["EXP-05", { dispatchMs: 60001 }, "dispatch threshold"],
  ["EXP-05", { steps: 4 }, "three-step budget"],
  ["EXP-06", { renderedGroups: [] }, "navigation-only inbox"],
  ["EXP-06", { totalVisible: 1 }, "dropped inbox item"],
  ["EXP-07", { member: false }, "long login or guest page"],
  ["EXP-07", { expectedTasks: [], renderedTasks: [] }, "empty page is not receiving a task"],
  ["EXP-07", { renderedTasks: [{ id: "other", title: "复盘任务" }] }, "task title belongs to wrong object"],
  ["EXP-07", { overflow: true }, "mobile overflow"],
  ["EXP-08", { moneyRedline: false }, "scope wording without money redline"],
  ["EXP-08", { expectedGrants: [], renderedGrants: [] }, "unseeded grant ledger"],
  ["EXP-08", { renderedGrants: [{ ...good["EXP-08"].renderedGrants[0], workspaces: "另一工作区" }] }, "wrong grant scope"],
  ["EXP-08", { renderedGrants: [{ ...good["EXP-08"].renderedGrants[0], capabilities: "业务写入" }] }, "wrong grant capability"],
  ["EXP-08", { renderedGrants: [{ ...good["EXP-08"].renderedGrants[0], expires: "2099/1/1" }] }, "wrong grant expiry"],
  ["EXP-08", { renderedGrants: [{ ...good["EXP-08"].renderedGrants[0], revoked: true }] }, "wrong grant revocation state"],
  ["EXP-09", { assistantAfter: 1 }, "old welcome is not a new answer"],
  ["EXP-09", { newAssistantText: "能提供发票吗？" }, "user echo"],
  ["EXP-09", { newAssistantText: "小虎正在思考…" }, "loading placeholder"],
  ["EXP-09", { requestOk: false }, "503 response"],
  ["EXP-09", { newAssistantComplete: false }, "unfinished typing"],
  ["EXP-09", { assistantAfterUser: false }, "old assistant before submitted user"],
  ["EXP-09", { firstReplyMs: 30001 }, "first reply threshold"],
  ["EXP-09", { replyMock: true, demoLabeled: false }, "unlabeled mock reply"],
  ["EXP-10", { reportVisible: false }, "button wording is not a report"],
  ["EXP-10", { reportAbsentBefore: false }, "pre-existing report is not the result of this replay"],
  ["EXP-10", { responseOk: false }, "failed replay request"],
  ["EXP-10", { reportRuleId: "other" }, "report belongs to other rule"],
  ["EXP-10", { afterRulesDigest: "changed" }, "unconfirmed rule persisted"],
  ["EXP-10", { afterApprovalsDigest: "changed" }, "unconfirmed approval persisted"],
  ["EXP-10", { confirmRequests: 1 }, "confirmation sent accidentally"],
];
for (const [id, patch, name] of falseGreens) {
  test(`${id}: reject ${name}`, async () => {
    const { evaluateJourney } = await helper();
    assert.equal(evaluateJourney(id, { ...clone(good[id]), ...patch }, thresholds).pass, false);
  });
}

function html(body, script = "") {
  return `<!doctype html><meta charset="utf-8"><style>body{font:16px sans-serif;margin:12px}button,input,textarea{min-height:32px}article{margin:10px;padding:8px;border:1px solid #888}main{max-width:100%}</style>${body}<script>
    const rpc=async(name,input,write=false)=>{const r=await fetch('/trpc/'+name+(write?'':'?input='+encodeURIComponent(JSON.stringify(input??{}))),{method:write?'POST':'GET',headers:{'content-type':'application/json'},body:write?JSON.stringify(input):undefined});return (await r.json()).result?.data};
    ${script}</script>`;
}

async function runFixture(name, kind, mode, onlyIds = null) {
  const temp = mkdtempSync(join(tmpdir(), "workloom-ui-evidence-"));
  const output = process.env.MC031_EVIDENCE_DIR ? join(resolve(process.env.MC031_EVIDENCE_DIR), name) : join(temp, "evidence");
  mkdirSync(output, { recursive: true });
  const state = { submitted: null, approvalStatus: "pending", requests: [], port: null };
  const bad = mode === "spoof";
  const agent = { id: "agent-1", preset_key: "fixture-agent", name: "测试岗位", version: "v1", fence_bindings: [], skills: [], meta: {} };
  const today = new Date().toISOString();
  const approvalRows = () => [
    { approval_id: "approval-1", event_id: "event-1", status: state.approvalStatus, created_at: today },
    ...(bad ? Array.from({ length: 7 }, (_, i) => ({ approval_id: `approval-${i + 2}`, event_id: `event-${i + 2}`, status: "pending", created_at: today })) : []),
  ];
  const thread = () => ({ id: "T-1", title: state.submitted ?? "本地复盘任务", status: "running", progress_total: 1, progress_done: 0 });
  const events = () => [{ event_id: "event-1", who: { type: "agent", id: "fixture" }, context: { time: today }, decision: { action: "quest.plan", after: { steps: [{ tool: "local.read", description: "读取并形成复盘" }] } } }];
  const rules = [{ id: "rule-row", rule_id: "R1", version: "v1", status: "active", name: "本地规则", match_spec: { object_types: ["local"], actions: ["local.read"], when: "true" } }];
  const versions = [{ version: "v1", status: "active", rules: "1", created_at: today }];
  const server = createServer(async (req, res) => {
    let payload = "";
    for await (const chunk of req) payload += chunk;
    const url = new URL(req.url, "http://127.0.0.1");
    let input = {};
    try { input = JSON.parse(payload || url.searchParams.get("input") || "{}"); }
    catch {
      res.writeHead(400, { "content-type": "application/json" });
      res.end(JSON.stringify({ error: "malformed fixture input" }));
      return;
    }
    state.requests.push({ method: req.method, path: url.pathname, input });
    if (url.pathname.startsWith("/trpc/") || url.pathname === "/c/chat") {
      const procedure = url.pathname.slice("/trpc/".length);
      let data;
      if (procedure === "approvals.list") data = approvalRows();
      else if (procedure === "approvals.decide") {
        if (!bad) state.approvalStatus = "approved";
        data = bad ? { error: "failed" } : { approvalId: input.approvalId, status: "approved", gestureEventId: "gesture-1", deduped: false };
      } else if (procedure === "captain.theater") data = { latestBriefing: { text: "今日经营简报：模拟任务运行正常", at: today }, pendingByTier: { l4_chairman: 1 } };
      else if (procedure === "captain.chairmanQueue") data = [{ approval_id: "approval-1", snapshot: { title: "需要决策的本地事项" } }];
      else if (procedure === "threads.list") data = [thread()];
      else if (procedure === "threads.get") data = thread();
      else if (procedure === "threads.events") data = events();
      else if (procedure === "threads.dispatch") {
        state.submitted = input.title;
        data = { kind: "routed", threadId: "T-1" };
      } else if (procedure === "accounts.inbox.unified") data = { groups: [{ workspaceId: "fixture", workspaceName: "模拟工作区", pendingApprovals: 1 }] };
      else if (procedure === "accounts.my.memberships") data = [{ workspace_id: "fixture", workspace_name: "模拟工作区", tenant_id: "tenant-fixture" }];
      else if (procedure === "access.me" || procedure === "members.me") data = { subject: { kind: bad ? "guest" : "member", memberNo: "MEM-001", name: "验收成员" }, scope: { workspaceId: "fixture" }, identity: { workspaceId: "fixture", memberNo: "MEM-001", name: "验收成员" } };
      else if (procedure === "accounts.admin.grants") data = [{ id: "grant-1", partner_name: "本地伙伴", workspaces: ["fixture"], capabilities: ["report.view"], expires_at: "2027-01-01" }];
      else if (procedure === "fence.rules") data = rules;
      else if (procedure === "fence.versions") data = versions;
      else if (procedure === "fence.dryRun") data = bad ? null : { dryRunId: "dry-1", report: { ruleId: input.ruleId, replayed: 1, wouldBlock: [], wouldReview: [], unchanged: 1, impact: "已回放 1 条，无新增拦截" } };
      else if (url.pathname === "/c/chat") {
        if (bad) {
          res.writeHead(503, { "content-type": "application/json" });
          res.end(JSON.stringify({ error: "service unavailable" }));
          return;
        }
        res.writeHead(200, { "content-type": "application/json" });
        res.end(JSON.stringify({ answer: "可以协助核对，请提供开票抬头。", conversationId: "conversation-1", mock: true }));
        return;
      } else data = {};
      res.writeHead(bad && ["approvals.decide", "fence.dryRun"].includes(procedure) ? 503 : 200, { "content-type": "application/json" });
      res.end(JSON.stringify({ result: { data } }));
      return;
    }
    res.writeHead(200, { "content-type": "text/html" });
    if (url.pathname.startsWith("/agents/")) {
      const heading = mode === "spoof-heading" ? "员工档案 · " : "员工档案 · 测试岗位";
      res.end(html(`<main><h2>${heading}<span>第 1 版</span></h2><section>身份与归属 第 1 版 来源行业包</section>${mode === "spoof-heading" ? "<p>成员档案 · 测试岗位</p>" : ""}</main>`));
    } else if (url.pathname === "/skills") {
      res.end(html("<main>官方技能 1 已安装</main>"));
    } else if (url.pathname === "/tasks/T-1") {
      res.end(html(`<main><h2>任务执行</h2><span>T-1</span><p>${state.submitted ?? "本地复盘任务"}</p><article>数字员工 · 事件 event-1 读取并形成复盘 步骤 拆解 回执 执行</article></main>`, `rpc('threads.get',{threadId:'T-1'});${mode === "stale-trace" ? "" : "rpc('threads.events',{threadId:'T-1'});"}`));
    } else if (url.pathname === "/tasks") {
      res.end(html(bad
        ? `<main><h1>任务</h1><p>登录已失效，请输入验证码。${"安全说明，当前任务等待身份验证。".repeat(40)}</p></main>`
        : `<main><h1>任务</h1><input placeholder="说出一句话目标…（最多 500 字）"><button id="dispatch">启航</button><div id="tasks"><article><h2>${thread().title}</h2><p>请求回执：T-1</p></article></div></main>`,
      bad ? `rpc('access.me');` : `rpc('access.me');rpc('threads.list');document.querySelector('#dispatch').onclick=async()=>{const title=document.querySelector('input').value;const r=await rpc('threads.dispatch',{title,presetKey:'fixture'},true);document.querySelector('#tasks').innerHTML='<a href="/tasks/'+r.threadId+'">'+title+'</a>';};`));
    } else if (url.pathname === "/approvals") {
      res.end(html(`<main><h2>审批中心</h2><button>approval-1 必审</button><section><button id="advance">推进</button><button>校准</button><button>制动</button></section><div id="notice">${bad ? "审批决定已写入事件账本，已采纳" : ""}</div></main>`,
        `rpc('approvals.list');document.querySelector('#advance').onclick=async()=>{const r=await rpc('approvals.decide',{approvalId:'approval-1',gesture:'approve'},true);${bad ? "" : `document.querySelector('#notice').textContent='审批决定已写入事件账本，账本事件 '+r.gestureEventId;rpc('approvals.list');`}};`));
    } else if (url.pathname === "/events") {
      res.end(html(`<main><h1>事件账本</h1><article><p>数字员工 · 事件 event-1</p><button title="本地复盘任务" onclick="location.href='${bad ? "/wrong-task" : "/tasks/T-1"}'">本地复盘任务</button></article></main>`, `rpc('threads.list');rpc('threads.events',{threadId:'T-1'});`));
    } else if (url.pathname === "/wrong-task") {
      res.end(html("<main><h2>另一个任务</h2>决策链路 拆解 执行 步骤 回执 依据</main>"));
    } else if (url.pathname === "/reports" || url.pathname === "/night") {
      res.end(html(`<main><h1>经营报告</h1><div id="brake"><button onclick="arm()">紧急制动</button></div></main>`, `function arm(){document.querySelector('#brake').innerHTML='<button>确认制动（全端 ≤60s 生效）</button><button id="cancel">撤回</button>';document.querySelector('#cancel').onclick=()=>{${bad ? "" : `document.querySelector('#brake').innerHTML='<button onclick="arm()">紧急制动</button>';`}}}`));
    } else if (url.pathname === "/inbox") {
      res.end(html(bad ? "<nav>统一待办 审批 告警 工单</nav><main>正在加载</main>" : `<main><h1>统一待办</h1><p>您在 1 家店有 1 项待审批——点任意一家进入处理</p><button><div class="font-semibold">模拟工作区</div><div>模拟租户 · 我的角色：负责人</div><span>1 待审批</span></button></main>`, `rpc('accounts.inbox.unified');`));
    } else if (url.pathname === "/partners") {
      res.end(html(`<main><h1>伙伴授权</h1><section><h2>授权台账（1）</h2><article><span class="font-medium">本地伙伴</span><div>工作区：${mode === "wrong-partner-scope" ? "其他工作区" : "模拟工作区"}<br>能力：经营报表 · 有效期至 2027/1/1</div></article></section>${bad ? "" : "<p>资金相关能力不向伙伴开放；空工作区范围会被客户端和服务端同时拒绝。</p>"}</main>`, `rpc('accounts.admin.grants');rpc('accounts.my.memberships');`));
    } else if (url.pathname === "/guardrails") {
      res.end(html(`<main><h2>规则与权限</h2><p>生效范围 规则版本记录</p><textarea placeholder="填写规则名称或审批意图"></textarea><select><option>本地规则</option></select><button id="draft">生成结构化规则草稿</button><div id="preview"></div><div id="report">${mode === "stale-report" ? "<div>模拟回放报告</div><div>已回放 1 条，无新增拦截</div>" : ""}</div></main>`,
        `rpc('fence.rules');rpc('fence.versions');document.querySelector('#draft').onclick=()=>{document.querySelector('#preview').innerHTML='<p>草稿预览（确认后进入变更审批）</p><button id="dry">模拟回放最近 10 条事件</button>';document.querySelector('#dry').onclick=async()=>{const r=await rpc('fence.dryRun',{ruleId:'R2',name:document.querySelector('textarea').value,level:'review',objectTypes:['local'],actions:['local.read'],when:'true'},true);${bad ? "" : `document.querySelector('#report').innerHTML='<div>模拟回放报告</div><div>'+r.report.impact+'</div><button>确认并提交变更审批（审批前不生效）</button>';`}};};`));
    } else if (url.pathname === "/guest" || url.pathname === "/guest/") {
      res.end(html(`<main><div id="messages"><div><div class="rounded-tl-sm">欢迎咨询，这是旧欢迎消息。${"本地服务介绍。".repeat(45)}</div><button>转人工</button></div></div><textarea placeholder="请输入您的需求…"></textarea><button id="send">发送</button><a href="#tickets">工单</a></main>`,
        `document.querySelector('#send').onclick=async()=>{const text=document.querySelector('textarea').value;document.querySelector('#messages').insertAdjacentHTML('beforeend','<div class="rounded-br-sm">'+text+'</div>');${bad ? `document.querySelector('#messages').insertAdjacentHTML('beforeend','<div>小虎正在思考…</div>');` : ""}const r=await fetch('/c/chat',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text})});const data=await r.json();${bad ? "" : `document.querySelector('#messages').insertAdjacentHTML('beforeend','<div><div class="rounded-tl-sm">'+data.answer+'<span>演示</span></div><button>转人工</button></div>');`}};`));
    } else {
      res.end(html(`<main data-product-ready="true"><h1>经营主页</h1><p>今日经营简报：模拟任务运行正常</p><section>请您决策 · 1 件<p>需要决策的本地事项</p></section>${bad ? "<p>待决 待审批</p>" : ""}</main>`, `rpc('captain.theater');rpc('captain.chairmanQueue');`));
    }
  });
  await new Promise((resolveListen) => server.listen(0, "127.0.0.1", resolveListen));
  state.port = server.address().port;
  const base = `http://127.0.0.1:${state.port}`;
  const scripts = join(temp, "scripts");
  mkdirSync(join(scripts, "lib"), { recursive: true });
  const sourceName = kind === "ui" ? "ui-probe.mjs" : "experience.mjs";
  const source = readFileSync(join(SOURCE, sourceName), "utf8");
  writeFileSync(join(scripts, sourceName), source);
  if (existsSync(join(HERE, "ui-evidence.mjs"))) writeFileSync(join(scripts, "lib/ui-evidence.mjs"), readFileSync(join(HERE, "ui-evidence.mjs")));
  const journeys = ["first-value", "approval", "traceability", "brake", "dispatch", "inbox", "mobile-staff", "partner-scope", "guest-service", "dry-run"]
    .map((script, i) => ({ id: `EXP-${String(i + 1).padStart(2, "0")}`, script: `builtin:${script}` }));
  const profile = {
    identity: { workspaceSlug: "fixture", human: "MEM-001" }, workspaceId: "fixture",
    routes: { home: "/", tasks: "/tasks", approvals: "/approvals", events: "/events", reports: "/reports", inbox: "/inbox", partners: "/partners", guardrails: "/guardrails" },
    surfaces: { pcRoutes: [], bMobileRoutes: [], cRoutes: [] }, storage: {},
    thresholds: { ...thresholds, firstValueMs: 5000, guestFirstReplyMs: 800, dispatchMs: 3000, approvalMs: 3000 },
    journeys,
  };
  writeFileSync(join(scripts, "lib/profile.mjs"), `export const cliArgs=()=>({outDir:${JSON.stringify(output)},has:()=>false});export const findRepoRoot=()=>${JSON.stringify(temp)};export const loadProfile=()=>({profile:${JSON.stringify(profile)},warnings:[]});export const urlsOf=()=>(${JSON.stringify({ pc: base, api: base, bMobile: base, cMobile: `${base}/guest` })});`);
  writeFileSync(join(scripts, "lib/session.mjs"), `export const loginAsMember=async()=> 'fixture-only';export const productIdOf=()=> 'fixture';`);
  writeFileSync(join(temp, "product.manifest.json"), '{"productId":"fixture"}');
  const playwrightUrl = pathToFileURL(join(HERE, "playwright.mjs")).href;
  writeFileSync(join(scripts, "lib/playwright.mjs"), `
    import{loadChromium as real}from ${JSON.stringify(playwrightUrl)};
    export function loadChromium(){return {launch:async(options)=>{
      const browser=await real(${JSON.stringify(GOVERNANCE)}).launch({...options,...(process.env.MC031_BROWSER_CHANNEL?{channel:process.env.MC031_BROWSER_CHANNEL}:{})});
      const original=browser.newContext.bind(browser);
      browser.newContext=async(opts)=>{const context=await original(opts);await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
        const newPage=context.newPage.bind(context);context.newPage=async()=>{const page=await newPage();${mode === "screenshot-failure" ? "page.screenshot=async()=>{throw new Error('fixture screenshot write failed')};" : ""}const wait=page.waitForTimeout.bind(page);page.waitForTimeout=ms=>wait(Math.min(ms,15));const waitFunction=page.waitForFunction.bind(page);page.waitForFunction=(fn,arg,opts)=>waitFunction(fn,arg,{...opts,timeout:Math.min(opts?.timeout??500,500)});return page};return context};
      return browser;
    }}}
  `);
  mkdirSync(join(temp, "node_modules/pg"), { recursive: true });
  writeFileSync(join(temp, "node_modules/pg/package.json"), '{"type":"module","exports":"./index.mjs"}');
  writeFileSync(join(temp, "node_modules/pg/index.mjs"), `export default {Client:class{async connect(){} async end(){} async query(sql){return{rows:sql.includes('FROM workspaces')?[{id:'fixture',slug:'fixture'}]:sql.includes('FROM agents')?[${JSON.stringify(agent)}]:[]}}}};`);
  const argv = [join(scripts, sourceName)];
  if (kind !== "ui") argv.push("--only", (onlyIds ?? journeys.map((j) => j.id)).join(","));
  try {
    const child = spawn(process.execPath, argv, { cwd: temp, env: { ...process.env, DATABASE_URL: "", MC031_BROWSER_CHANNEL: process.env.MC031_BROWSER_CHANNEL ?? "" }, stdio: ["ignore", "pipe", "pipe"] });
    let log = "";
    child.stdout.on("data", (chunk) => { log += chunk; });
    child.stderr.on("data", (chunk) => { log += chunk; });
    const timer = setTimeout(() => child.kill("SIGKILL"), 120000);
    const exitCode = await new Promise((resolveExit, reject) => { child.once("error", reject); child.once("close", resolveExit); });
    clearTimeout(timer);
    writeFileSync(join(output, "fixture-process.log"), log);
    writeFileSync(join(output, "fixture-receipt.json"), JSON.stringify({ sourceName, sourceSha256: hash(source), port: state.port, bind: "127.0.0.1", exitCode, requests: state.requests, productDatabase: false, productPorts: false }, null, 2));
    const reportPath = join(output, kind === "ui" ? "ui-probe.json" : "experience-report.json");
    assert.ok(existsSync(reportPath), `${name}: CLI produced no report\n${log}`);
    return { report: JSON.parse(readFileSync(reportPath, "utf8")), requests: state.requests, exitCode };
  } finally {
    await new Promise((resolveClose) => server.close(resolveClose));
    rmSync(temp, { recursive: true, force: true });
  }
}

test("real browser CLI: employee title passes", { timeout: 120000 }, async () => {
  const { report, exitCode } = await runFixture("employee-heading", "ui", "positive");
  assert.equal(report.agents["fixture-agent"].ok, true);
  assert.equal(exitCode, 0);
});

test("real browser CLI: body text cannot impersonate a missing profile name", { timeout: 120000 }, async () => {
  const { report, exitCode } = await runFixture("spoof-heading", "ui", "spoof-heading");
  assert.equal(report.agents["fixture-agent"].ok, false);
  assert.equal(exitCode, 1);
});

const expectedJourneyIds = Object.keys(good);
function assertCompleteJourneys(report) {
  assert.equal(report.checks.length, expectedJourneyIds.length);
  assert.deepEqual(report.checks.map((check) => check.id).sort(), [...expectedJourneyIds].sort());
}

test("real browser CLI: failed actions and unrelated text never produce green journeys", { timeout: 120000 }, async (t) => {
  const { report, requests, exitCode } = await runFixture("spoofed-success", "experience", "spoof");
  assertCompleteJourneys(report);
  assert.equal(exitCode, 1);
  for (const check of report.checks) {
    await t.test(`${check.id} refuses spoofed success`, () => assert.equal(check.pass, false, JSON.stringify(check.actual)));
  }
  assert.equal(requests.some((r) => r.path === "/trpc/threads.dispatch"), false, "the browser had no task input: direct dispatch must not run");
});

test("real browser CLI: valid UI actions and their own results remain passable", { timeout: 120000 }, async (t) => {
  const { report, requests, exitCode } = await runFixture("bound-success", "experience", "positive");
  assertCompleteJourneys(report);
  for (const check of report.checks) {
    await t.test(`${check.id} accepts complete evidence`, () => assert.equal(check.pass, true, JSON.stringify(check.actual)));
  }
  assert.ok(requests.some((r) => r.path === "/trpc/threads.dispatch" && r.method === "POST"));
  assert.ok(requests.some((r) => r.path === "/c/chat" && r.method === "POST"));
  assert.equal(requests.some((r) => /confirmDryRun|nightShift.pause/.test(r.path)), false);
  assert.equal(exitCode, 0);
});

for (const [mode, id] of [["stale-trace", "EXP-03"], ["wrong-partner-scope", "EXP-08"], ["stale-report", "EXP-10"], ["screenshot-failure", "EXP-04"]]) {
  test(`real browser CLI: ${mode} cannot pass ${id}`, { timeout: 120000 }, async () => {
    const { report, exitCode } = await runFixture(mode, "experience", mode, [id]);
    assert.deepEqual(report.checks.map((check) => check.id), [id]);
    assert.equal(report.checks[0].pass, false, JSON.stringify(report.checks[0].actual));
    assert.equal(exitCode, 1);
    if (mode === "screenshot-failure") {
      assert.match(report.checks[0].actual.error, /screenshot write failed/);
      assert.equal(report.checks[0].evidence, undefined);
    }
  });
}

test("real browser CLI: UI screenshot failures make the probe unsuccessful", { timeout: 120000 }, async () => {
  const { report, exitCode } = await runFixture("ui-screenshot-failure", "ui", "screenshot-failure");
  assert.ok(report.issues.some((issue) => issue.where.startsWith("screenshot:")));
  assert.equal(exitCode, 1);
});
