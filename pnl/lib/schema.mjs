import { createHash } from 'node:crypto';

export class PnlError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

export const fail = (message, code = 'INVALID_RECORD') => { throw new PnlError(422, code, message); };
export const digest = value => createHash('sha256').update(typeof value === 'string' ? value : canonical(value)).digest('hex');
export function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value !== null && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return JSON.stringify(value);
}
function shape(value, keys, path) {
  if (!value || Array.isArray(value) || typeof value !== 'object' || Object.getPrototypeOf(value) !== Object.prototype) fail(`${path} must be an object`);
  if (Object.keys(value).length !== keys.length || keys.some(key => !Object.hasOwn(value, key))) fail(`${path} has missing or unsupported fields`);
}
function text(value, path, max = 200, pattern) {
  if (typeof value !== 'string' || !value.length || value.length > max || value.trim() !== value || /[\u0000-\u001f\u007f<>]/u.test(value) || (pattern && !pattern.test(value))) fail(`${path} must be valid plain text`);
}
function number(value, path, min = 0, max = 1e15) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) fail(`${path} is outside its numeric range`);
}
function time(value, path) {
  text(value, path, 40, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/);
  if (!Number.isFinite(Date.parse(value))) fail(`${path} must be an ISO timestamp with timezone`);
  // Date.parse normalizes impossible calendar dates; compare the literal date separately.
  const day = new Date(`${value.slice(0, 10)}T00:00:00Z`);
  if (day.toISOString().slice(0, 10) !== value.slice(0, 10)) fail(`${path} has an invalid calendar date`);
}
function list(value, path, max) { if (!Array.isArray(value) || value.length > max) fail(`${path} must be an array of at most ${max} items`); }
function unique(items, key, path) { if (new Set(items.map(item => item[key])).size !== items.length) fail(`${path} has duplicate ${key}`); }
const symbol = (value, path) => text(value, path, 20, /^[A-Z0-9][A-Z0-9.^=-]*$/);
export const validKey = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$/.test(value);
const near = (a, b) => Math.abs(a - b) <= Math.max(0.02, Math.abs(b) * 1e-8);

function validateDecisions(decisions, asOf) {
  list(decisions, 'decisions', 500);
  for (const d of decisions) {
    shape(d, ['id', 'symbol', 'action', 'targetWeight', 'thesis', 'decidedAt', 'runId'], 'decision');
    text(d.id, 'decision.id', 128); symbol(d.symbol, 'decision.symbol');
    if (!['buy', 'sell', 'hold', 'watch'].includes(d.action)) fail('decision.action is invalid');
    number(d.targetWeight, 'decision.targetWeight', 0, 1); text(d.thesis, 'decision.thesis', 2000);
    time(d.decidedAt, 'decision.decidedAt'); text(d.runId, 'decision.runId', 128);
    if (Date.parse(d.decidedAt) > Date.parse(asOf)) fail('decision.decidedAt must not be after asOf');
  }
  unique(decisions, 'id', 'decisions');
  if (decisions.reduce((sum, d) => sum + d.targetWeight, 0) > 1.000001) fail('decision target weights must sum to at most 1');
}

export function validateRecord(record, now = Date.now()) {
  shape(record, ['schemaVersion', 'idempotencyKey', 'asOf', 'account', 'positions', 'trades', 'decisions', 'netExternalFlow', 'provenance'], 'record');
  if (record.schemaVersion !== 1) fail('schemaVersion must be 1');
  if (!validKey(record.idempotencyKey)) fail('idempotencyKey must be 8-128 safe characters');
  time(record.asOf, 'asOf');
  if (Date.parse(record.asOf) > now + 5 * 60_000) fail('asOf cannot be in the future');
  shape(record.account, ['label', 'currency', 'nav', 'cash'], 'account');
  text(record.account.label, 'account.label', 80);
  if (record.account.currency !== 'USD') fail('Only USD accounts are supported');
  number(record.account.nav, 'account.nav', 0.01);
  number(record.account.cash, 'account.cash');
  number(record.netExternalFlow, 'netExternalFlow', -1e15);
  list(record.positions, 'positions', 500);
  for (const p of record.positions) {
    shape(p, ['symbol', 'name', 'quantity', 'avgCost', 'price', 'marketValue'], 'position');
    symbol(p.symbol, 'position.symbol'); text(p.name, 'position.name', 120);
    number(p.quantity, 'position.quantity', Number.MIN_VALUE, 1e12);
    number(p.avgCost, 'position.avgCost'); number(p.price, 'position.price', Number.MIN_VALUE);
    number(p.marketValue, 'position.marketValue', Number.MIN_VALUE);
    if (!near(p.marketValue, p.quantity * p.price)) fail('position.marketValue must equal quantity × price');
  }
  unique(record.positions, 'symbol', 'positions');
  if (!near(record.account.nav, record.account.cash + record.positions.reduce((sum, p) => sum + p.marketValue, 0))) fail('account.nav must equal cash plus position market values');
  list(record.trades, 'trades', 2000);
  for (const trade of record.trades) {
    shape(trade, ['id', 'executedAt', 'symbol', 'side', 'quantity', 'price', 'fees'], 'trade');
    text(trade.id, 'trade.id', 128); time(trade.executedAt, 'trade.executedAt'); symbol(trade.symbol, 'trade.symbol');
    if (!['buy', 'sell'].includes(trade.side)) fail('trade.side must be buy or sell');
    number(trade.quantity, 'trade.quantity', Number.MIN_VALUE, 1e12); number(trade.price, 'trade.price', Number.MIN_VALUE); number(trade.fees, 'trade.fees');
    if (Date.parse(trade.executedAt) > Date.parse(record.asOf)) fail('trade.executedAt must not be after asOf');
  }
  unique(record.trades, 'id', 'trades');
  validateDecisions(record.decisions, record.asOf);
  shape(record.provenance, ['kind', 'evidenceSha256', 'sourceRef', 'attestedBy'], 'provenance');
  if (!['broker_statement', 'operator_attested'].includes(record.provenance.kind)) fail('Only real account evidence is accepted', 'REAL_EVIDENCE_REQUIRED');
  text(record.provenance.evidenceSha256, 'provenance.evidenceSha256', 64, /^[a-f0-9]{64}$/);
  if (/^0+$/.test(record.provenance.evidenceSha256)) fail('Evidence hash cannot be a placeholder', 'REAL_EVIDENCE_REQUIRED');
  text(record.provenance.sourceRef, 'provenance.sourceRef', 200); text(record.provenance.attestedBy, 'provenance.attestedBy', 120);
  if (/(?:^|[\s/_.-])(?:paper|sim|simulated|simulation|mock|demo|synthetic)(?:$|[\s/_.-])/i.test(record.provenance.sourceRef)) fail('Paper or simulated sources cannot be published as real accounts', 'REAL_EVIDENCE_REQUIRED');
  return record;
}

export function validateResearch(record, now = Date.now()) {
  shape(record, ['schemaVersion', 'idempotencyKey', 'asOf', 'runId', 'receiptHash', 'dataAsOf', 'executionMode', 'modelMode', 'status', 'universeSize', 'summary', 'decisions'], 'research');
  if (record.schemaVersion !== 1) fail('schemaVersion must be 1');
  if (!validKey(record.idempotencyKey)) fail('idempotencyKey must be 8-128 safe characters');
  time(record.asOf, 'asOf');
  if (Date.parse(record.asOf) > now + 5 * 60_000) fail('asOf cannot be in the future');
  text(record.runId, 'runId', 128); text(record.receiptHash, 'receiptHash', 64, /^[a-f0-9]{64}$/);
  if (/^0+$/.test(record.receiptHash)) fail('receiptHash cannot be a placeholder');
  if (typeof record.dataAsOf === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(record.dataAsOf)) time(`${record.dataAsOf}T00:00:00Z`, 'dataAsOf');
  else time(record.dataAsOf, 'dataAsOf');
  if (Date.parse(record.dataAsOf) > Date.parse(record.asOf)) fail('dataAsOf cannot be after asOf');
  if (record.executionMode !== 'paper') fail('Research must disclose the controlled paper workflow');
  if (!['disabled', 'enabled', 'passthrough'].includes(record.modelMode)) fail('modelMode is invalid');
  if (!['completed', 'completed_degraded'].includes(record.status)) fail('Only independently verified completed research may be published', 'RESEARCH_NOT_COMPLETED');
  number(record.universeSize, 'universeSize', 0, 100000);
  if (!Number.isInteger(record.universeSize)) fail('universeSize must be an integer');
  text(record.summary, 'summary', 10000); validateDecisions(record.decisions, record.asOf);
  if (record.decisions.some(d => d.runId !== record.runId)) fail('Research decisions must refer to this verified runId');
  return record;
}
