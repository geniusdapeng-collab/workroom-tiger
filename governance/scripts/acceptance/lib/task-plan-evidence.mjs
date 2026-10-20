/**
 * Observe the existing detail poll and the real plan list. This helper never
 * sends a query, dispatches or resumes a task, clicks, or resets a time budget.
 */
const normalized = (value) => typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";

export async function taskPlanDom(page) {
  return page.evaluate(() => {
    const section = document.querySelector('main section[aria-label="任务拆解"]');
    const visible = (node) => {
      if (!node || node.getBoundingClientRect().width <= 0 || node.getBoundingClientRect().height <= 0) return false;
      for (let current = node; current; current = current.parentElement) {
        const style = getComputedStyle(current);
        if (current.hidden || style.display === "none" || style.visibility !== "visible" || Number(style.opacity) === 0) return false;
      }
      return true;
    };
    return {
      pathname: location.pathname,
      threadId: section?.getAttribute("data-thread-id") ?? null,
      stale: section?.getAttribute("data-plan-stale") !== "false",
      visible: visible(section),
      labels: section ? [...section.querySelectorAll("ol > li")].filter(visible).map((item) => item.textContent ?? "") : [],
    };
  });
}

export function taskPlanEvidence({ detail, dom, threadId, title, navigationAt, deadline, expectedPath }) {
  const bound = Boolean(normalized(threadId) && normalized(title)) && detail?.ok === true && detail.procedure === "threads.get"
    && detail.input?.threadId === threadId && detail.data?.id === threadId && detail.data?.title === title
    && Number.isFinite(detail.requestAt) && detail.requestAt >= navigationAt
    && Number.isFinite(detail.at) && detail.at >= detail.requestAt && detail.at <= deadline;
  const plan = bound && detail.data?.plan_state === "ready" && Array.isArray(detail.data?.plan) ? detail.data.plan : [];
  const ids = new Set();
  const valid = plan.length > 0 && plan.every((step) => {
    if (!step || !normalized(step.stepId) || !normalized(step.label) || ids.has(step.stepId)) return false;
    ids.add(step.stepId);
    return true;
  });
  const visible = valid && dom?.healthy === true && dom.visible === true && dom.stale === false
    && dom.pathname === expectedPath && dom.threadId === threadId
    && Array.isArray(dom.labels) && dom.labels.length === plan.length
    && plan.every((step, index) => normalized(step.label) === normalized(dom.labels[index]));
  return { detail: bound ? detail : null, plan: valid ? plan : [], planVisible: Boolean(visible) };
}

/** The observer appends by completion time; a delayed old poll must not replace
 * a more recent request already rendered by P2. Keep failed/new pending rows:
 * choosing only successful ready responses would fabricate freshness. */
export function latestTaskPlanResponse(responses, threadId, navigationAt) {
  let latest = null;
  for (const row of responses) {
    if (row.procedure !== "threads.get" || row.input?.threadId !== threadId
      || !Number.isFinite(row.requestAt) || row.requestAt < navigationAt) continue;
    if (!latest || row.requestAt > latest.requestAt
      || row.requestAt === latest.requestAt && row.at >= latest.at) latest = row;
  }
  return latest;
}

export async function waitForTaskPlan({
  watcher, threadId, title, navigationAt, dispatchAt, deadline, expectedPath, readDom,
  now = () => Date.now(), sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
}) {
  let result = { detail: null, plan: [], planVisible: false, dispatchMs: null };
  if (![navigationAt, dispatchAt, deadline].every(Number.isFinite) || navigationAt < dispatchAt || deadline < navigationAt) return result;
  while (now() <= deadline) {
    const detail = latestTaskPlanResponse(watcher.responses, threadId, navigationAt);
    const dom = await readDom();
    const at = now();
    const evidence = taskPlanEvidence({ detail, dom, threadId, title, navigationAt, deadline, expectedPath });
    result = { ...evidence, planVisible: evidence.planVisible && at <= deadline, dispatchMs: null };
    if (result.planVisible) return { ...result, dispatchMs: at - dispatchAt };
    const remaining = deadline - at;
    if (remaining <= 0) break;
    await sleep(Math.min(100, remaining));
  }
  return result;
}
