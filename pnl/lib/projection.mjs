export const round = value => Math.round((value + Number.EPSILON) * 1e8) / 1e8;
export function publicRecord(entry) {
  const r = entry.record;
  return {
    sequence: entry.sequence, hash: entry.hash, asOf: r.asOf,
    account: { currency: r.account.currency, nav: r.account.nav, cash: r.account.cash },
    positions: r.positions.map(p => ({ symbol: p.symbol, name: p.name, quantity: p.quantity, avgCost: p.avgCost, price: p.price, marketValue: p.marketValue, weight: round(p.marketValue / r.account.nav), unrealizedPnl: round(p.marketValue - p.quantity * p.avgCost) })),
    trades: r.trades.map(t => ({ id: t.id, executedAt: t.executedAt, symbol: t.symbol, side: t.side, quantity: t.quantity, price: t.price, fees: t.fees })),
    decisions: r.decisions.map(d => ({ id: d.id, symbol: d.symbol, action: d.action, targetWeight: d.targetWeight, thesis: d.thesis, decidedAt: d.decidedAt, runId: d.runId })),
  };
}
export function publicResearch(entry) {
  const r = entry.record;
  return { sequence: entry.sequence, hash: entry.hash, asOf: r.asOf, runId: r.runId, receiptHash: r.receiptHash, dataAsOf: r.dataAsOf, executionMode: r.executionMode, modelMode: r.modelMode, status: r.status, universeSize: r.universeSize, summary: r.summary, decisions: r.decisions.map(d => ({ id: d.id, symbol: d.symbol, action: d.action, targetWeight: d.targetWeight, thesis: d.thesis, decidedAt: d.decidedAt, runId: d.runId })) };
}
export function dashboard(entries, now = new Date().toISOString(), researchEntries = []) {
  const result = { schemaVersion: 1, status: entries.length ? 'active' : 'awaiting_first_record', generatedAt: now, baseline: null, latest: null, metrics: null, history: [], records: [], research: { latest: researchEntries.length ? publicResearch(researchEntries.at(-1)) : null, history: researchEntries.slice().reverse().map(publicResearch) } };
  if (!entries.length) return result;
  let unitValue = 1; let peak = 1;
  for (let i = 0; i < entries.length; i++) {
    const r = entries[i].record;
    if (i) unitValue *= (r.account.nav - r.netExternalFlow) / entries[i - 1].record.account.nav;
    peak = Math.max(peak, unitValue);
    result.history.push({ asOf: r.asOf, nav: r.account.nav, returnPct: round((unitValue - 1) * 100), drawdownPct: round((unitValue / peak - 1) * 100) });
  }
  const latest = publicRecord(entries.at(-1)); const last = result.history.at(-1);
  result.baseline = { date: entries[0].record.asOf, nav: entries[0].record.account.nav };
  result.latest = latest;
  result.metrics = { returnPct: last.returnPct, drawdownPct: last.drawdownPct, unrealizedPnl: round(latest.positions.reduce((sum, p) => sum + p.unrealizedPnl, 0)), positionCount: latest.positions.length, recordCount: entries.length, netExternalFlow: round(entries.slice(1).reduce((sum, entry) => sum + entry.record.netExternalFlow, 0)) };
  result.records = entries.slice(-20).reverse().map(publicRecord);
  return result;
}
