/**
 * Pure acceptance predicates. A screenshot or a successful click is not a
 * business receipt: collectors must bind the request, object and rendered result.
 * Missing evidence remains unverified and can never become a passing journey.
 */
const nonempty = (value) => typeof value === "string" && value.trim().length > 0;
const text = (value) => String(value ?? "").replace(/\s+/g, " ").trim();
const compact = (value) => text(value).replace(/\s+/g, "");
const known = (value, predicate = Boolean) => value === undefined || value === null ? null : Boolean(predicate(value));
const bool = (value) => known(value, (v) => v === true);
const same = (left, right) => left == null || right == null ? null : nonempty(left) && left === right;
const duration = (value, limit) => known(value, (v) => Number.isFinite(v) && v >= 0 && Number.isFinite(limit) && v <= limit);
const count = (value) => Number.isInteger(value) && value >= 0;

/** DOM collectors supply only visible page/status text here. Chat messages are
 * content, not application-state signals, even when an answer says it cannot
 * help. The matching request and completed reply are checked separately. */
export function pageStateEvidence(snapshot) {
  const loadingCopy = /^(?:正在恢复工作区|正在确认身份|正在确认访问范围|正在确认行业能力|正在连接(?:\s*WorkLoom\s*)?服务|正在加载|加载中)/;
  const errorCopy = /^(?:登录已失效|请重新登录|B 端移动工作台|暂时无法|服务连接失败|服务配置待恢复|加载失败|读取失败|当前身份不能访问|Internal Server Error|WorkLoom\s*(?:服务暂时无法连接|环境未就绪))/;
  const direct = Array.isArray(snapshot.directTexts) ? snapshot.directTexts : [];
  const states = Array.isArray(snapshot.stateTexts) ? snapshot.stateTexts : [];
  const loading = snapshot.loadingState === true || [...direct, ...states].some((value) => loadingCopy.test(text(value)));
  const terminalError = snapshot.errorState === true || [...direct, ...states].some((value) => errorCopy.test(text(value)));
  return { healthy: nonempty(snapshot.text) && !loading && !terminalError, loading, terminalError, text: snapshot.text ?? "", ready: snapshot.ready === true };
}

/** loginAsMember obtains TOKEN with auth.loginAs(profile.identity.workspaceSlug).
 * members.me is read with that same TOKEN; its server-verified identity is the
 * slug-to-ID witness. Never derive the expected ID from the access under test. */
export function workspaceIdentityEvidence(profile, identity, access) {
  const requestedSlug = profile.identity?.workspaceSlug ?? null;
  const memberNo = profile.identity?.human ?? "MEM-001";
  const declaredWorkspaceId = profile.workspaceId ?? null;
  const evidence = {
    source: "auth.loginAs(workspaceSlug) -> members.me(same token) -> access.me",
    requestedSlug, memberNo, declaredWorkspaceId,
    resolvedTenantId: identity?.tenantId ?? null,
    resolvedWorkspaceId: identity?.workspaceId ?? null,
    observedTenantId: access?.scope?.tenantId ?? null,
    observedWorkspaceId: access?.scope?.workspaceId ?? null,
  };
  if (!nonempty(requestedSlug) || !identity || !nonempty(identity.tenantId) || !nonempty(identity.workspaceId)
    || !nonempty(identity.memberNo) || !access || !nonempty(access.scope?.tenantId) || !nonempty(access.scope?.workspaceId)) {
    return { ...evidence, matches: null, reason: "缺少登录工作区或服务端身份/范围证据" };
  }
  const matches = identity.memberNo === memberNo && access.subject?.kind === "member" && access.subject.memberNo === memberNo
    && (declaredWorkspaceId === null || nonempty(declaredWorkspaceId) && declaredWorkspaceId === identity.workspaceId)
    && identity.tenantId === access.scope.tenantId && identity.workspaceId === access.scope.workspaceId;
  return { ...evidence, matches, reason: matches ? null : "声明、登录身份或移动端范围不一致" };
}

export function profileEvidence(dom, expected) {
  const match = /^(?:员工|成员)档案\s*·\s*(\S(?:.*\S)?)$/.exec(text(dom.heading));
  const headerOk = Boolean(match && match[1] === text(expected.name));
  const identityOk = headerOk && String(dom.identity ?? "").includes(expected.versionLabel)
    && String(dom.identity ?? "").includes("来源行业包");
  const fenceOk = !dom.dangling && count(dom.declared) && dom.declared >= expected.expectedFences;
  const skillsOk = !dom.uninstalled && count(dom.equipped) && dom.equipped >= expected.expectedSkills;
  return { ok: identityOk && fenceOk && skillsOk && dom.notFound === false, headerOk, identityOk, fenceOk, skillsOk };
}

/** Parse one tRPC exchange, including its index in an HTTP batch. */
export function rpcExchange(url, method, postData, status, response, procedure) {
  let parsed;
  try { parsed = new URL(url); } catch { return null; }
  const marker = parsed.pathname.indexOf("/trpc/");
  if (marker < 0) return null;
  const names = decodeURIComponent(parsed.pathname.slice(marker + 6)).split(",");
  const index = names.indexOf(procedure);
  if (index < 0) return null;
  const batched = parsed.searchParams.get("batch") === "1";
  let inputs = postData;
  if (method === "GET") {
    try { inputs = JSON.parse(parsed.searchParams.get("input") ?? "null"); }
    catch { return { procedure, input: null, data: null, status, ok: false }; }
  }
  const input = batched ? inputs?.[index] : inputs;
  const envelope = batched ? response?.[index] : response;
  const data = envelope?.result?.data;
  return {
    procedure, input: input ?? null, data: data ?? null, status,
    ok: status >= 200 && status < 300 && !envelope?.error && data !== undefined && data !== null,
  };
}

/** approvals.list returns the latest 50 rows. Only a complete local-day window
 * can prove the daily quota; duplicated notification channels count once. */
export function dailyDecisionEvidence(rows, at = new Date()) {
  const start = new Date(at); start.setHours(0, 0, 0, 0);
  const end = new Date(start); end.setDate(end.getDate() + 1);
  const result = { count: null, complete: false, dayStart: start.toISOString(), dayEnd: end.toISOString(), timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone };
  if (!Array.isArray(rows)) return result;
  const times = rows.map((row) => new Date(row?.created_at).getTime());
  if (rows.some((row) => !nonempty(row?.event_id)) || times.some((time) => !Number.isFinite(time) || time > at.getTime())) return result;
  const ids = new Set(rows.filter((_, i) => times[i] >= start.getTime() && times[i] < end.getTime()).map((row) => row.event_id));
  return { ...result, count: ids.size, complete: rows.length < 50 || times.some((time) => time < start.getTime()) };
}

export function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

/** Generic labels are not identifiers. Numeric suffixes are accepted only as
 * distinct tokens; callers must reject collisions within the source response. */
export function visibleIdentifier(value, id) {
  if (!nonempty(id)) return false;
  const escaped = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (new RegExp(`(^|[^A-Za-z0-9_-])${escaped}(?=$|[^A-Za-z0-9_-])`).test(text(value))) return true;
  const suffix = /(\d+)$/.exec(id)?.[1];
  return Boolean(suffix && new RegExp(`···${suffix}(?!\\d)`).test(text(value)));
}

function sameRows(expected, actual, matches) {
  if (!Array.isArray(expected) || !Array.isArray(actual)) return null;
  if (expected.length !== actual.length) return false;
  const remaining = [...actual];
  for (const row of expected) {
    const index = remaining.findIndex((candidate) => matches(row, candidate));
    if (index < 0) return false;
    remaining.splice(index, 1);
  }
  return true;
}

export function evaluateJourney(id, a, thresholds) {
  const checks = { "页面未处于登录、加载或错误态": bool(a.healthy) };
  switch (id) {
    case "EXP-01":
      Object.assign(checks, {
        "当前经营事实在主内容可见": bool(a.valueVisible),
        "欢迎仪式确已跳过": a.ceremony === false ? true : a.ceremony == null ? null : bool(a.skipSucceeded),
        "首价值耗时达标": duration(a.firstValueMs, thresholds.firstValueMs),
        "当日决策范围完整": bool(a.dailyCountComplete),
        "当日决策件数达标": known(a.decisionCount, (v) => count(v) && v <= thresholds.decisionQuota),
      });
      break;
    case "EXP-02":
      Object.assign(checks, {
        "推进手势可见": bool(a.gestures?.advance), "校准手势可见": bool(a.gestures?.calibrate), "制动手势可见": bool(a.gestures?.brake),
        "推进真实点击": bool(a.clicked),
        "请求绑定选中审批": same(a.selectedApprovalId, a.requestApprovalId),
        "回执绑定选中审批": same(a.selectedApprovalId, a.receiptApprovalId),
        "回执含账本事件": known(a.gestureEventId, nonempty),
        "回执状态已批准": known(a.approvalStatus, (v) => v === "approved"),
        "页面显示本次回执": bool(a.receiptVisible),
        "服务端回读已批准": known(a.readbackStatus, (v) => v === "approved"),
        "单件耗时达标": duration(a.elapsedMs, thresholds.approvalMs),
      });
      break;
    case "EXP-03":
      Object.assign(checks, {
        "原事件有编号": known(a.eventId, nonempty), "原卡执行者可见": bool(a.cardActor), "原卡事件编号可见": bool(a.cardEventVisible),
        "关联入口真实点击": bool(a.clicked),
        "目标任务身份一致": same(a.threadId, a.destinationThreadId),
        "目标链包含原事件": bool(a.destinationHasEvent),
        "点击次数达标": known(a.clicks, (v) => Number.isInteger(v) && v > 0 && v <= thresholds.traceClicks),
      });
      break;
    case "EXP-04":
      Object.assign(checks, {
        "制动入口可执行": bool(a.entryEnabled), "确认层实际打开": bool(a.opened), "确认层生效范围可见": bool(a.scopeVisible),
        "撤回真实点击": bool(a.cancelled), "确认层已关闭并恢复入口": bool(a.closed),
        "未提交暂停请求": known(a.pauseRequests, (v) => v === 0),
      });
      break;
    case "EXP-05":
      Object.assign(checks, {
        "输入入口可见": bool(a.inputVisible), "输入填写成功": bool(a.filled), "启航真实点击": bool(a.clicked),
        "请求绑定输入目标": same(a.title, a.requestTitle), "服务端成功响应": bool(a.responseOk),
        "响应含新任务编号": known(a.threadId, nonempty), "详情任务编号一致": same(a.threadId, a.detailThreadId),
        "详情任务目标一致": same(a.title, a.detailTitle), "实际拆解非空": known(a.planSteps, (v) => Number.isInteger(v) && v > 0),
        "拆解在页面可见": bool(a.planVisible), "三步内完成": known(a.steps, (v) => Number.isInteger(v) && v > 0 && v <= 3),
        "派发耗时达标": duration(a.dispatchMs, thresholds.dispatchMs),
      });
      break;
    case "EXP-06": {
      const groups = a.expectedGroups;
      Object.assign(checks, {
        "统一待办标题可见": bool(a.titleVisible), "聚合来源读取成功": bool(a.sourceOk),
        "各工作区事项数一致": sameRows(groups, a.renderedGroups, (expected, actual) => nonempty(expected.workspaceName)
          && expected.workspaceName === actual.workspaceName && count(expected.pendingApprovals) && expected.pendingApprovals === actual.pendingApprovals),
        "总数与来源一致": !Array.isArray(groups) || a.totalVisible == null ? null : count(a.totalVisible)
          && groups.every((g) => count(g.pendingApprovals)) && groups.reduce((sum, g) => sum + g.pendingApprovals, 0) === a.totalVisible,
        "空态有下一步": !Array.isArray(groups) ? null : groups.length > 0 || a.emptyNextStep === true,
      });
      break;
    }
    case "EXP-07":
      Object.assign(checks, {
        "成员身份已确认": bool(a.member), "工作区一致": bool(a.workspaceMatches), "任务标题可见": bool(a.titleVisible),
        "任务来源读取成功": bool(a.sourceOk), "存在真实任务": known(a.expectedTasks, (v) => Array.isArray(v) && v.length > 0),
        "任务对象与内容一致": sameRows(a.expectedTasks, a.renderedTasks, (expected, actual) => nonempty(expected.id) && expected.id === actual.id && nonempty(expected.title) && expected.title === actual.title),
        "无横向溢出": known(a.overflow, (v) => v === false),
      });
      break;
    case "EXP-08":
      Object.assign(checks, {
        "伙伴授权标题可见": bool(a.titleVisible), "授权来源读取成功": bool(a.sourceOk),
        "存在可核对授权": known(a.expectedGrants, (v) => Array.isArray(v) && v.length > 0),
        "授权范围能力期限逐条可见": sameRows(a.expectedGrants, a.renderedGrants, (expected, actual) => nonempty(expected.name)
          && expected.name === actual.name && nonempty(expected.workspaces) && nonempty(expected.capabilities) && nonempty(expected.expires)
          && expected.workspaces === actual.workspaces && expected.capabilities === actual.capabilities && expected.expires === actual.expires
          && expected.revoked === actual.revoked),
        "资金红线明确可见": bool(a.moneyRedline),
      });
      break;
    case "EXP-09":
      Object.assign(checks, {
        "咨询输入可见": bool(a.inputVisible), "发送真实点击": bool(a.clicked),
        "请求绑定本次问题": same(a.question, a.requestText), "聊天响应成功": bool(a.requestOk),
        "响应包含会话编号": known(a.conversationId, nonempty),
        "新增用户气泡": a.userBefore == null || a.userAfter == null ? null : count(a.userBefore) && a.userAfter === a.userBefore + 1,
        "用户气泡内容一致": same(a.question, a.newUserText),
        "新增助手气泡": a.assistantBefore == null || a.assistantAfter == null ? null : count(a.assistantBefore) && a.assistantAfter === a.assistantBefore + 1,
        "助手气泡位于本次问题之后": bool(a.assistantAfterUser),
        "助手回复完成": bool(a.newAssistantComplete),
        "助手回复绑定本次响应": a.newAssistantText == null || a.responseText == null ? null : nonempty(a.responseText)
          && compact(a.newAssistantText) === compact(a.responseText) && compact(a.newAssistantText) !== compact(a.question)
          && !/正在思考|发送失败|连接失败|加载中|正在连接/.test(a.newAssistantText),
        "首响耗时达标": duration(a.firstReplyMs, thresholds.guestFirstReplyMs),
        "新回复下可转人工": bool(a.replyHasHandoff), "工单入口可达": bool(a.ticketsEntry),
        "模拟回复明确标记": a.replyMock == null ? null : a.replyMock === false || a.demoLabeled === true,
      });
      break;
    case "EXP-10":
      Object.assign(checks, {
        "草稿编辑器可见": bool(a.editorVisible), "草稿按钮真实点击": bool(a.draftClicked), "新草稿预览可见": bool(a.draftVisible),
        "回放按钮真实点击": bool(a.clicked), "回放响应成功": bool(a.responseOk), "回放含编号": known(a.dryRunId, nonempty),
        "回放绑定当前规则": same(a.requestedRuleId, a.reportRuleId), "新报告节点可见": bool(a.reportVisible), "回放前无旧报告": bool(a.reportAbsentBefore),
        "报告匹配回放响应": bool(a.reportTextMatches),
        "未确认规则未改变": same(a.beforeRulesDigest, a.afterRulesDigest),
        "未确认版本未改变": same(a.beforeVersionsDigest, a.afterVersionsDigest),
        "未确认审批未改变": same(a.beforeApprovalsDigest, a.afterApprovalsDigest),
        "未提交确认请求": known(a.confirmRequests, (v) => v === 0),
      });
      break;
    default:
      checks["旅程判据已注册"] = false;
  }
  const missing = Object.entries(checks).filter(([, value]) => value === null).map(([name]) => name);
  const failed = Object.entries(checks).filter(([, value]) => value === false).map(([name]) => name);
  return { pass: missing.length === 0 && failed.length === 0, status: missing.length ? "unverified" : failed.length ? "failed" : "passed", checks, missing, failed };
}
