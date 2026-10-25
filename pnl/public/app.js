"use strict";

(() => {
  const USD = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const NUMBER = new Intl.NumberFormat("en-US", { maximumFractionDigits: 6 });
  const PERCENT = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const DATE_TIME = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
  const DATE = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" });
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ACTION_NAMES = { buy: "买入计划", sell: "卖出计划", hold: "继续持有", watch: "保持观察" };
  let dashboard = null;
  let range = "all";
  let historyExpanded = false;
  let requestPending = false;
  let lastAttemptAt = 0;

  const byId = (id) => document.getElementById(id);
  const finite = (value) => typeof value === "number" && Number.isFinite(value);
  const money = (value) => finite(value) ? USD.format(value) : "—";
  const number = (value) => finite(value) ? NUMBER.format(value) : "—";
  const percent = (value, signed = false) => finite(value) ? `${signed && value > 0 ? "+" : ""}${PERCENT.format(Object.is(value, -0) ? 0 : value)}%` : "—";
  const validDate = (value) => typeof value === "string" && Number.isFinite(Date.parse(value));
  const dateTime = (value) => validDate(value) ? `${DATE_TIME.format(new Date(value))}（北京时间）` : "截至时间未提供";
  const dateOnly = (value) => validDate(value) ? DATE.format(new Date(value)) : "—";

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function svgElement(tag, attributes = {}, text) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function setMetric(id, text, numericValue) {
    const node = byId(id);
    node.textContent = text;
    node.classList.toggle("is-positive", finite(numericValue) && numericValue > 0);
    node.classList.toggle("is-negative", finite(numericValue) && numericValue < 0);
  }

  function emptyRow(targetId, columns, message) {
    const row = element("tr");
    const cell = element("td", "table-empty");
    cell.colSpan = columns;
    cell.append(element("span", "table-empty-message", message));
    row.append(cell);
    byId(targetId).replaceChildren(row);
  }

  function validateDashboard(data) {
    if (!data || data.schemaVersion !== 1 || !["awaiting_first_record", "active"].includes(data.status)) throw new Error("INVALID_SCHEMA");
    if (!["history", "records"].every((key) => Array.isArray(data[key]))) throw new Error("INVALID_HISTORY");
    if (data.research !== undefined) {
      if (!data.research || !Array.isArray(data.research.history)) throw new Error("INVALID_RESEARCH");
      const research = data.research.latest;
      if (research !== null) {
        if (!research || !validDate(research.asOf) || !validDate(research.dataAsOf) || research.executionMode !== "paper" || !["disabled", "enabled", "passthrough"].includes(research.modelMode) || !["completed", "completed_degraded"].includes(research.status) || !finite(research.universeSize) || !Array.isArray(research.decisions)) throw new Error("INVALID_RESEARCH_RECORD");
        validateDecisions(research.decisions);
      }
    }
    if (data.status === "awaiting_first_record") {
      if (data.latest !== null || data.baseline !== null || data.metrics !== null || data.history.length) throw new Error("INVALID_EMPTY_STATE");
      return data;
    }
    const latest = data.latest;
    if (!latest || !validDate(latest.asOf) || !latest.account || latest.account.currency !== "USD" || !finite(latest.account.nav) || latest.account.nav <= 0 || !finite(latest.account.cash)) throw new Error("INVALID_ACCOUNT");
    if (!data.baseline || !validDate(data.baseline.date) || !finite(data.baseline.nav) || data.baseline.nav <= 0 || !data.metrics || !["returnPct", "drawdownPct", "recordCount"].every((key) => finite(data.metrics[key]))) throw new Error("INVALID_METRICS");
    for (const key of ["positions", "trades", "decisions"]) if (!Array.isArray(latest[key])) throw new Error("INVALID_ENTRIES");
    for (const row of data.history) if (!validDate(row.asOf) || !["nav", "returnPct", "drawdownPct"].every((key) => finite(row[key]))) throw new Error("INVALID_HISTORY_ENTRY");
    for (const row of latest.positions) if (typeof row.symbol !== "string" || !["quantity", "avgCost", "price", "marketValue", "weight", "unrealizedPnl"].every((key) => finite(row[key])) || row.weight < 0 || row.weight > 1) throw new Error("INVALID_POSITION");
    for (const row of latest.trades) if (typeof row.symbol !== "string" || !["buy", "sell"].includes(row.side) || !validDate(row.executedAt) || !["quantity", "price", "fees"].every((key) => finite(row[key]))) throw new Error("INVALID_TRADE");
    validateDecisions(latest.decisions);
    return data;
  }

  function validateDecisions(decisions) {
    for (const row of decisions) if (typeof row.symbol !== "string" || !Object.hasOwn(ACTION_NAMES, row.action) || !validDate(row.decidedAt) || typeof row.thesis !== "string" || !finite(row.targetWeight) || row.targetWeight < 0 || row.targetWeight > 1) throw new Error("INVALID_DECISION");
  }

  function renderChartState(title, description, loading = false) {
    const container = byId("performance-chart");
    container.classList.toggle("has-chart", false);
    const state = element("div", "chart-state");
    state.append(element(loading ? "span" : "div", loading ? "loading-ring" : "chart-empty-symbol", loading ? undefined : "∅"));
    state.firstChild.setAttribute("aria-hidden", "true");
    state.append(element("p", "", title));
    if (description) state.append(element("span", "", description));
    container.replaceChildren(state);
    container.setAttribute("aria-label", `${title}。${description || ""}`);
  }

  function renderAwaiting(error = false) {
    for (const id of ["metric-nav", "metric-return", "metric-drawdown"]) setMetric(id, "—");
    byId("metric-days").replaceChildren(document.createTextNode("—"), element("small", "", "天"));
    byId("nav-note").textContent = "来自真实账户快照";
    byId("return-note").textContent = "自首笔真实记录起";
    byId("drawdown-note").textContent = "记录期内净值高点至低点";
    byId("days-note").textContent = "第一天，从真实开始";
    byId("baseline-date").textContent = error ? "基准日期暂不可用" : "基准日期待建立";
    byId("stock-weight").textContent = "—";
    byId("cash-weight").textContent = "—";
    byId("position-count").textContent = error ? "持仓数据暂不可用" : "持仓记录待入账";
    byId("allocation-bar").replaceChildren();
    byId("allocation-bar").setAttribute("aria-label", "资产配置暂无数据");
    byId("history-toggle").hidden = true;
    document.querySelectorAll("[data-range]").forEach((button) => { button.disabled = true; });
    byId("account-status").textContent = error ? "账簿暂不可用" : "待首笔真实记录";
    byId("account-status").className = `state-label${error ? " is-error" : ""}`;
    byId("as-of").textContent = error ? "当前无法获取账户数据" : "真实记录入账后开始计时";
    byId("chart-subtitle").textContent = "每一个数据点，对应一笔真实账户记录。";
    renderChartState(error ? "账簿暂时未能读取" : "首笔真实账户记录待入账", error ? "连接恢复后重新读取，请稍后重试。" : "基准尚未建立，业绩从第一笔真实记录开始。" );
    emptyRow("history-body", 5, error ? "历史账页暂不可用" : "第一张账页，等待真实记录。");
    emptyRow("positions-body", 6, error ? "持仓数据暂不可用" : "首笔真实账户快照入账后公开持仓。");
    byId("history-count").textContent = error ? "/ —" : "/ 0 笔";
    byId("positions-as-of").textContent = error ? "当前无法获取账户快照" : "截至时间待更新";
    byId("decisions-list").replaceChildren(element("p", "journal-empty", error ? "决策记录暂不可用。" : "暂无已公开的研究决策。"));
    byId("trades-list").replaceChildren(element("p", "journal-empty", error ? "成交记录暂不可用。" : "暂无已入账的真实成交。"));
  }

  function renderActive(data) {
    const { latest, metrics, baseline } = data;
    const start = Date.parse(`${dateOnly(baseline.date)}T00:00:00Z`);
    const end = Date.parse(`${dateOnly(latest.asOf)}T00:00:00Z`);
    const days = Math.max(1, Math.floor((end - start) / 86400000) + 1);
    byId("account-status").textContent = data.history.length === 1 ? "真实账户 · 基准已建立" : "真实账户 · 持续记账";
    byId("account-status").className = "state-label is-active";
    byId("as-of").textContent = `截至 ${dateTime(latest.asOf)}`;
    setMetric("metric-nav", money(latest.account.nav));
    setMetric("metric-return", percent(metrics.returnPct, true), metrics.returnPct);
    const maxDrawdown = data.history.reduce((maximum, record) => Math.min(maximum, record.drawdownPct), 0);
    setMetric("metric-drawdown", percent(maxDrawdown), maxDrawdown);
    byId("metric-days").replaceChildren(document.createTextNode(number(days)), element("small", "", "天"));
    byId("days-note").textContent = `始于 ${dateOnly(baseline.date)}`;
    byId("nav-note").textContent = `账户记账币种 · ${latest.account.currency}`;
    byId("return-note").textContent = data.history.length === 1 ? "首日建立基准，收益从此累计" : "已剔除记录期内净出入金影响";
    byId("drawdown-note").textContent = data.history.length === 1 ? "首日基准，尚无跨日回撤" : "逐期收益净值的最大回落";
    byId("baseline-date").textContent = `基准 ${dateOnly(baseline.date)} · ${money(baseline.nav)}`;
    byId("history-count").textContent = `/ ${number(metrics.recordCount)} 笔`;
    byId("positions-as-of").textContent = dateTime(latest.asOf);
    byId("position-count").textContent = latest.positions.length ? `${latest.positions.length} 只股票持仓` : "当前账户未持有股票";
    byId("methodology-text").textContent = "首笔真实账户记录建立基准。相邻快照先从本期净资产中扣除本期净入金，计算单期收益，再逐期复合；资金流采用期末口径，不把入金算成盈利。";
    byId("methodology-detail").textContent = "收益包含已记录的费用；最大回撤按剔除资金流影响后的收益净值计算。样本不足时不展示年化收益或胜率。";
    renderAllocation(latest);
    renderPositions(latest.positions);
    renderTrades(latest.trades);
    renderHistory();
    renderChart();
    document.querySelectorAll("[data-range]").forEach((button) => { button.disabled = false; });
  }

  function renderAllocation(latest) {
    const positionValue = latest.positions.reduce((total, row) => total + row.marketValue, 0);
    const stockWeight = positionValue / latest.account.nav;
    const cashWeight = latest.account.cash / latest.account.nav;
    byId("stock-weight").textContent = percent(stockWeight * 100);
    byId("cash-weight").textContent = percent(cashWeight * 100);
    const bar = byId("allocation-bar");
    const svg = svgElement("svg", { viewBox: "0 0 1000 5", preserveAspectRatio: "none", "aria-hidden": "true" });
    bar.replaceChildren(svg);
    bar.setAttribute("aria-label", `股票持仓 ${percent(stockWeight * 100)}，现金 ${percent(cashWeight * 100)}`);
    let offset = 0;
    for (const [weight, color] of [[stockWeight, "#fa6a32"], [cashWeight, "#abb59d"]]) {
      if (weight <= 0) continue;
      const segmentWidth = Math.max(0, Math.min(weight, 1)) * 1000;
      svg.append(svgElement("rect", { x: offset, y: 0, width: segmentWidth, height: 5, fill: color }));
      offset += segmentWidth;
    }
  }

  function renderPositions(positions) {
    if (!positions.length) {
      emptyRow("positions-body", 6, "当前账户未持有股票。现金同样是账户配置的一部分。" );
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of [...positions].sort((a, b) => b.weight - a.weight)) {
      const row = element("tr");
      const asset = element("td");
      asset.append(element("span", "asset-symbol", item.symbol), element("span", "asset-name", item.name || item.symbol));
      const price = element("td", "numeric", money(item.price));
      price.append(element("span", "price-subvalue", `成本 ${money(item.avgCost)}`));
      const weightCell = element("td", "numeric");
      const weight = element("span", "weight-cell");
      const track = svgElement("svg", { class: "weight-track", viewBox: "0 0 42 3", preserveAspectRatio: "none" });
      track.setAttribute("aria-hidden", "true");
      track.append(svgElement("rect", { width: Math.min(42, Math.max(0, item.weight * 42)), height: 3, fill: "#fa6a32" }));
      weight.append(track, document.createTextNode(percent(item.weight * 100)));
      weightCell.append(weight);
      row.append(asset, element("td", "numeric", number(item.quantity)), price, element("td", "numeric", money(item.marketValue)), weightCell, element("td", `numeric${item.unrealizedPnl > 0 ? " is-positive" : item.unrealizedPnl < 0 ? " is-negative" : ""}`, `${item.unrealizedPnl > 0 ? "+" : ""}${money(item.unrealizedPnl)}`));
      fragment.append(row);
    }
    byId("positions-body").replaceChildren(fragment);
  }

  function renderHistory() {
    const history = [...dashboard.history].sort((a, b) => Date.parse(b.asOf) - Date.parse(a.asOf));
    const rows = historyExpanded ? history : history.slice(0, 7);
    const dayCounts = new Map();
    for (const record of history) { const day = dateOnly(record.asOf); dayCounts.set(day, (dayCounts.get(day) || 0) + 1); }
    const fragment = document.createDocumentFragment();
    for (const item of rows) {
      const row = element("tr");
      const date = element("td", "table-date", dateOnly(item.asOf));
      const sameDayCount = dayCounts.get(dateOnly(item.asOf));
      if (sameDayCount > 1) date.append(element("small", "", `${DATE_TIME.format(new Date(item.asOf)).split(" ").pop()} 更新`));
      const status = element("td", "record-column");
      status.append(element("span", "record-label", item === history[history.length - 1] ? "首笔基准" : "账户记录"));
      row.append(date, element("td", "numeric", money(item.nav)), element("td", `numeric${item.returnPct > 0 ? " is-positive" : item.returnPct < 0 ? " is-negative" : ""}`, percent(item.returnPct, true)), element("td", "numeric", percent(item.drawdownPct)), status);
      fragment.append(row);
    }
    byId("history-body").replaceChildren(fragment);
    const toggle = byId("history-toggle");
    toggle.hidden = history.length <= 7;
    toggle.textContent = historyExpanded ? "收起历史记录 ↑" : `展开全部 ${history.length} 笔记录 ↓`;
    toggle.setAttribute("aria-expanded", String(historyExpanded));
  }

  function renderResearch(data) {
    const research = data.research?.latest;
    const stamp = byId("research-stamp");
    stamp.hidden = !research;
    stamp.replaceChildren();
    if (research) {
      stamp.append(element("span", "", `研究更新 ${dateTime(research.asOf)}`), element("span", "", `行情截至 ${dateOnly(research.dataAsOf)} · 覆盖 ${number(research.universeSize)} 只标的`));
      if (research.modelMode === "disabled") stamp.append(element("span", "research-disclosure", "本轮未启用模型，研究按公开行情与系统规则生成。"));
      else if (research.modelMode === "passthrough") stamp.append(element("span", "research-disclosure", "本轮模型环节未产生完整结论，研究结果存在降级。"));
      renderDecisions(research.decisions, true);
    } else renderDecisions(data.latest?.decisions || []);
  }

  function renderDecisions(decisions, hasResearch = false) {
    const list = byId("decisions-list");
    if (!decisions.length) {
      list.replaceChildren(element("p", "journal-empty", hasResearch ? "本轮研究未给出新的股票买卖计划，账户实际配置以持仓记录为准。" : "截至该快照，暂无已公开的研究决策。"));
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of [...decisions].sort((a, b) => Date.parse(b.decidedAt) - Date.parse(a.decidedAt))) {
      const article = element("article", "decision-entry");
      const header = element("div", "decision-entry-head");
      header.append(element("span", `action-badge action-${item.action}`, ACTION_NAMES[item.action]), element("h4", "", item.symbol), element("span", "decision-target", `目标仓位 ${percent(item.targetWeight * 100)}`));
      const meta = element("div", "entry-meta");
      meta.append(element("time", "", dateTime(item.decidedAt)), element("span", "", item.runId ? `记录 ${item.runId}` : "研究决策记录"));
      meta.firstChild.dateTime = item.decidedAt;
      article.append(header, element("p", "decision-thesis", item.thesis), meta);
      fragment.append(article);
    }
    list.replaceChildren(fragment);
  }

  function renderTrades(trades) {
    const list = byId("trades-list");
    if (!trades.length) {
      list.replaceChildren(element("p", "journal-empty", "截至该快照，暂无已入账的真实成交。"));
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const item of [...trades].sort((a, b) => Date.parse(b.executedAt) - Date.parse(a.executedAt))) {
      const article = element("article", "trade-entry");
      const side = element("span", `trade-side${item.side === "sell" ? " sell" : ""}`, item.side === "buy" ? "买" : "卖");
      const main = element("div", "trade-main");
      const time = element("time", "", dateTime(item.executedAt));
      time.dateTime = item.executedAt;
      const detail = element("small");
      detail.append(time);
      main.append(element("strong", "", item.symbol), detail);
      const value = element("div", "trade-value", `${number(item.quantity)} 股 × ${money(item.price)}`);
      value.append(element("small", "", `费用 ${money(item.fees)}`));
      article.append(side, main, value);
      fragment.append(article);
    }
    list.replaceChildren(fragment);
  }

  function renderChart() {
    let history = [...dashboard.history].sort((a, b) => Date.parse(a.asOf) - Date.parse(b.asOf));
    if (range !== "all" && history.length) {
      const threshold = Date.parse(history[history.length - 1].asOf) - (Number(range) - 1) * 86400000;
      history = history.filter((row) => Date.parse(row.asOf) >= threshold);
    }
    document.querySelectorAll("[data-range]").forEach((button) => {
      const selected = button.dataset.range === range;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    if (!history.length) {
      renderChartState("该时段暂无账户记录", "调整时间范围查看已公开的账页。" );
      return;
    }
    const container = byId("performance-chart");
    const mobile = window.matchMedia("(max-width: 740px)").matches;
    const width = mobile ? Math.max(270, container.clientWidth || 300) : 1100;
    const height = mobile ? 235 : 270;
    const padding = { top: 30, right: mobile ? 12 : 24, bottom: 34, left: mobile ? 40 : 57 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const svg = svgElement("svg", { class: "chart-svg", viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", "aria-hidden": "true" });
    let min = history.reduce((value, point) => Math.min(value, point.returnPct), 0);
    let max = history.reduce((value, point) => Math.max(value, point.returnPct), 0);
    const spread = max - min;
    const margin = spread < .02 ? 1 : spread * .22;
    min -= margin;
    max += margin;
    const y = (value) => padding.top + (max - value) / (max - min) * plotHeight;
    const startTime = Date.parse(history[0].asOf);
    const endTime = Date.parse(history[history.length - 1].asOf);
    const x = (value) => history.length === 1 || endTime === startTime ? padding.left + plotWidth / 2 : padding.left + (Date.parse(value) - startTime) / (endTime - startTime) * plotWidth;
    for (let i = 0; i < 4; i += 1) {
      const value = max - (max - min) * i / 3;
      const position = y(value);
      svg.append(svgElement("line", { x1: padding.left, x2: width - padding.right, y1: position, y2: position, stroke: "#e9ece1", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }));
      svg.append(svgElement("text", { x: padding.left - 12, y: position + 3, class: "chart-label", "text-anchor": "end" }, `${value.toFixed(1)}%`));
    }
    svg.append(svgElement("line", { x1: padding.left, x2: width - padding.right, y1: y(0), y2: y(0), stroke: "#c6cebb", "stroke-dasharray": "3 5", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }));
    const tooltip = element("div", "chart-tooltip");
    tooltip.setAttribute("aria-hidden", "true");
    if (history.length > 1) {
      const path = history.map((point, index) => `${index === 0 ? "M" : "L"}${x(point.asOf).toFixed(2)},${y(point.returnPct).toFixed(2)}`).join(" ");
      svg.append(svgElement("path", { d: path, fill: "none", stroke: "#fa6a32", "stroke-width": 2.3, "stroke-linecap": "round", "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" }));
    }
    for (const point of history) {
      const dot = svgElement("g");
      dot.append(svgElement("circle", { cx: x(point.asOf), cy: y(point.returnPct), r: history.length === 1 ? 6 : 3.5, fill: "#fa6a32", stroke: "#fffefa", "stroke-width": 2, "vector-effect": "non-scaling-stroke" }));
      dot.append(svgElement("circle", { cx: x(point.asOf), cy: y(point.returnPct), r: 14, fill: "transparent" }));
      const label = `${dateOnly(point.asOf)} · 净资产 ${money(point.nav)} · 累计收益 ${percent(point.returnPct, true)}`;
      dot.append(svgElement("title", {}, label));
      dot.addEventListener("pointerenter", () => { tooltip.textContent = label; });
      dot.addEventListener("pointerleave", () => { tooltip.textContent = ""; });
      svg.append(dot);
    }
    if (history.length === 1) {
      const point = history[0];
      svg.append(svgElement("text", { x: x(point.asOf), y: y(point.returnPct) - 24, class: "chart-point-label", "text-anchor": "middle" }, dashboard.history.length === 1 ? "第 1 天 · 基准建立" : "该时段 1 笔记录"));
      svg.append(svgElement("text", { x: x(point.asOf), y: y(point.returnPct) + 29, class: "chart-point-note", "text-anchor": "middle" }, "长期结果，从这一点开始。"));
    }
    const labelIndexes = history.length === 1 ? [0] : [0, ...(history.length > 3 ? [Math.floor((history.length - 1) / 2)] : []), history.length - 1];
    for (const index of labelIndexes) {
      const point = history[index];
      svg.append(svgElement("text", { x: x(point.asOf), y: height - 8, class: "chart-label", "text-anchor": history.length === 1 ? "middle" : index === 0 ? "start" : index === history.length - 1 ? "end" : "middle" }, dateOnly(point.asOf)));
    }
    container.replaceChildren(svg, tooltip);
    container.classList.add("has-chart");
    container.setAttribute("aria-label", history.length === 1 ? `${dateOnly(history[0].asOf)}，${dashboard.history.length === 1 ? "首笔真实记录建立基准" : "该时段唯一记录"}，账户净资产 ${money(history[0].nav)}，累计收益 ${percent(history[0].returnPct)}。详细数据见下方每日账页。` : `${dateOnly(history[0].asOf)} 至 ${dateOnly(history[history.length - 1].asOf)}，共 ${history.length} 笔真实记录，最新累计收益 ${percent(history[history.length - 1].returnPct, true)}。详细数据见下方每日账页。`);
    byId("chart-subtitle").textContent = dashboard.history.length === 1 ? "首日只有一个真实数据点，持续记账后形成收益曲线。" : `按已公开账户快照连接，共 ${history.length} 笔记录；并非实时行情。`;
  }

  function showConnectionNotice(message) {
    const notice = byId("connection-notice");
    const button = element("button", "retry-button", "重新读取");
    button.type = "button";
    button.addEventListener("click", () => { void loadDashboard(); });
    notice.replaceChildren(element("span", "", message), button);
    notice.hidden = false;
  }

  async function loadDashboard() {
    if (requestPending) return;
    requestPending = true;
    lastAttemptAt = Date.now();
    byId("performance").setAttribute("aria-busy", "true");
    const retry = document.querySelector(".retry-button");
    if (retry) { retry.disabled = true; retry.textContent = "读取中…"; }
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch("/api/public/dashboard", { method: "GET", headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin", signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP_${response.status}`);
      const data = validateDashboard(await response.json());
      dashboard = data;
      byId("connection-notice").hidden = true;
      if (data.status === "active") renderActive(data); else renderAwaiting();
      renderResearch(data);
    } catch (error) {
      if (!dashboard) renderAwaiting(true);
      showConnectionNotice(dashboard?.status === "active" ? "最新账簿暂时无法读取，当前保留上次成功读取的记录，请以标注的截至时间为准。" : "账户数据暂时无法读取。页面不展示未经核实的业绩，请重新读取。" );
    } finally {
      window.clearTimeout(timeout);
      requestPending = false;
      byId("performance").setAttribute("aria-busy", "false");
    }
  }

  byId("copyright-year").textContent = String(new Date().getFullYear());
  byId("history-toggle").addEventListener("click", () => { historyExpanded = !historyExpanded; renderHistory(); });
  document.querySelectorAll("[data-range]").forEach((button) => {
    button.addEventListener("click", () => { range = button.dataset.range; renderChart(); });
  });
  window.addEventListener("online", () => { void loadDashboard(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - lastAttemptAt >= 300000) void loadDashboard(); });
  window.setInterval(() => { if (!document.hidden) void loadDashboard(); }, 300000);
  let resizeTimer;
  window.addEventListener("resize", () => {
    window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => { if (dashboard?.status === "active") renderChart(); }, 150);
  });
  void loadDashboard();
})();
