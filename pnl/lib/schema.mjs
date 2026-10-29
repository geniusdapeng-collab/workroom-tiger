import { createHash } from 'node:crypto';
import { UPGRADE_PROFILE, UPGRADE_PROFILES, MODEL_STAGES_V2, exact, isoTime, publicUrl, validateModelV2, validateThesisV2, validDate } from '../../bundles/trading/capabilities/required-v2-contract.mjs';
export { UPGRADE_PROFILE, UPGRADE_PROFILES, MODEL_STAGES_V2 };
export const DEFAULT_MODEL_PROFILE = UPGRADE_PROFILE;

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
  text(value, path, 40, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/);
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

export const MODEL_PROFILE = 'us-public-model-required-paper-v1';
export const REQUIRED_MODEL_STAGES = Object.freeze(['clean.llm_semantic', 'tech.sentiment', 'tech.risk', 'sector.narrative', 'decision.debate']);
export function validateModelEvidence(model) {
  if (model?.research_contract === 'tiger.short-term-research.v2') {
    try { return validateModelV2(model); } catch (cause) { fail(cause.message, 'MODEL_REQUIRED'); }
  }
  shape(model, ['provider', 'model', 'release', 'required', 'response_ids', 'call_count', 'required_stages', 'stage_counts', 'search_count', 'search_mode', 'all_required_completed'], 'model');
  if (model.provider !== 'deepseek' || model.model !== 'deepseek-flash' || model.release !== 'DeepSeek-V4.1-Flash-1017' || model.required !== true || model.all_required_completed !== true || model.search_mode !== 'provider_native_search') fail('A completed required DeepSeek model assessment is required', 'MODEL_REQUIRED');
  list(model.required_stages, 'model.required_stages', REQUIRED_MODEL_STAGES.length);
  if (model.required_stages.length !== REQUIRED_MODEL_STAGES.length || new Set(model.required_stages).size !== REQUIRED_MODEL_STAGES.length || REQUIRED_MODEL_STAGES.some(stage => !model.required_stages.includes(stage))) fail('Every required semantic stage must be completed by the model', 'MODEL_REQUIRED');
  shape(model.stage_counts, REQUIRED_MODEL_STAGES, 'model.stage_counts');
  for (const stage of REQUIRED_MODEL_STAGES) {
    number(model.stage_counts[stage], `model.stage_counts.${stage}`, 1, 100);
    if (!Number.isSafeInteger(model.stage_counts[stage])) fail('Model stage counts must be integers', 'MODEL_REQUIRED');
  }
  number(model.call_count, 'model.call_count', REQUIRED_MODEL_STAGES.length, 500);
  number(model.search_count, 'model.search_count', 14, 14);
  if (!Number.isSafeInteger(model.call_count) || !Number.isSafeInteger(model.search_count) || model.call_count !== Object.values(model.stage_counts).reduce((sum, count) => sum + count, 0)) fail('Model response counts must reconcile with completed stages', 'MODEL_REQUIRED');
  list(model.response_ids, 'model.response_ids', 500);
  if (model.response_ids.length !== model.call_count || new Set(model.response_ids).size !== model.call_count) fail('Every completed model response requires a unique provider identity', 'MODEL_REQUIRED');
  for (const id of model.response_ids) text(id, 'model.response_id', 160, /^[A-Za-z0-9][A-Za-z0-9_.:-]*$/);
  return model;
}
function validateSources(sources, v3 = false) {
  list(sources, 'instruction.sources', v3 ? 16 : 8);
  for (const source of sources) {
    shape(source, ['id', 'title', 'url', 'published_at', ...(v3 ? ['fetched_at', 'method', 'content_sha256'] : [])], 'instruction.source');
    text(source.id, 'instruction.source.id', 100); text(source.title, 'instruction.source.title', 500); text(source.url, 'instruction.source.url', 2048);
    if (v3) {
      number(source.fetched_at, 'instruction.source.fetched_at');
      if (!['deepseek_search', 'deepseek_fetch'].includes(source.method)) fail('Source acquisition method is invalid');
      text(source.content_sha256, 'instruction.source.content_sha256', 64, /^[a-f0-9]{64}$/);
    }
    if (source.id.startsWith('md_')) fail('Market evidence cannot be presented as a news article');
    let url;
    try { url = new URL(source.url); } catch { fail('Source URL is invalid'); }
    if (!['https:', 'http:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.hash || [...url.searchParams.keys()].some(key => /token|key|signature|authorization|credential|secret|password/i.test(key.replace(/[_-]/g, '')))) fail('Source URLs must be public links without credentials');
    if (source.published_at !== null) {
      text(source.published_at, 'instruction.source.published_at', 80);
      if (!Number.isFinite(Date.parse(source.published_at))) fail('Source publication time is invalid');
    }
  }
  unique(sources, 'id', 'instruction.sources');
}
function validateMarketEvidence(evidence, ownSymbol, assessmentDate) {
  list(evidence, 'instruction.market_evidence', 1);
  if (evidence.length !== 1) fail('Every instruction requires its own frozen stock market snapshot');
  const snapshot = evidence[0];
  shape(snapshot, ['id', 'symbol', 'as_of', 'close', 'series_sha256'], 'instruction.market_evidence');
  symbol(snapshot.symbol, 'instruction.market_evidence.symbol');
  if (snapshot.symbol !== ownSymbol) fail('Market snapshot must match the instruction symbol');
  text(snapshot.as_of, 'instruction.market_evidence.as_of', 10, /^\d{4}-\d{2}-\d{2}$/);
  if (snapshot.as_of.startsWith('0000-')) fail('Market snapshot requires a valid calendar year');
  time(`${snapshot.as_of}T00:00:00Z`, 'instruction.market_evidence.as_of');
  if (snapshot.as_of > assessmentDate) fail('Market snapshot cannot be after the New York assessment date');
  number(snapshot.close, 'instruction.market_evidence.close', Number.MIN_VALUE, Number.MAX_VALUE);
  text(snapshot.series_sha256, 'instruction.market_evidence.series_sha256', 64, /^[a-f0-9]{64}$/);
  if (/^0+$/.test(snapshot.series_sha256)) fail('Market series hash cannot be a placeholder');
  const expectedId = `md_${snapshot.symbol}_${snapshot.as_of.replaceAll('-', '')}_${snapshot.series_sha256.slice(0, 24)}`;
  if (snapshot.id !== expectedId) fail('Market evidence ID must bind its symbol, date and frozen series hash');
}
function validateExecutionDecisions(instructions, decisions, asOf, record = null) {
  const v3 = record?.schemaVersion === 3;
  list(instructions, 'executionDecisions', 500);
  if (instructions.length !== decisions.length) fail('Model instructions must match the published decision set');
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(asOf)).map(({ type, value }) => [type, value]));
  const assessmentDate = `${parts.year}-${parts.month}-${parts.day}`;
  for (const item of instructions) {
    shape(item, ['symbol', 'action', 'target_weight', 'action_summary', 'why_this', 'why_now', 'entry_condition', 'invalidation', 'risk_note', 'sources', 'market_evidence', 'entry_price', 'stop_price', 'shares', 'risk_usd', 'time_stop_days', 'valid_for', 'reference_capital_usd', ...(v3 ? ['thesis'] : [])], 'executionDecision');
    symbol(item.symbol, 'executionDecision.symbol');
    if (!['buy', 'watch'].includes(item.action)) fail('Required-model instructions support buy or watch only');
    const decision = decisions.find(row => row.symbol === item.symbol);
    if (!decision || decision.action !== item.action || decision.targetWeight !== item.target_weight) fail('Execution instruction differs from the published action or allocation');
    for (const key of ['action_summary', 'why_this', 'why_now', 'entry_condition', 'invalidation', 'risk_note']) text(item[key], `executionDecision.${key}`, 2000);
    time(item.valid_for, 'executionDecision.valid_for');
    validateSources(item.sources, v3);
    validateMarketEvidence(item.market_evidence, item.symbol, assessmentDate);
    for (const key of ['entry_price', 'stop_price', 'shares', 'risk_usd', 'time_stop_days', 'reference_capital_usd']) {
      if (item[key] !== null) number(item[key], `executionDecision.${key}`, Number.MIN_VALUE);
    }
    if ((item.shares !== null && !Number.isSafeInteger(item.shares)) || (item.time_stop_days !== null && !Number.isSafeInteger(item.time_stop_days))) fail('Shares and time stop must be whole numbers');
    if (item.action === 'buy' && ((!v3 && !item.sources.length) || item.target_weight <= 0 || item.entry_price === null || item.stop_price === null || item.shares === null || item.risk_usd === null || item.reference_capital_usd === null || item.stop_price >= item.entry_price)) fail('Buy instructions need news evidence and verified entry, stop, size and risk facts');
    if (v3) {
      try {
        const research = record.candidate_research;
        const known = new Set([...research.source_index.map(row => row.id), ...research.fact_evidence.map(row => row.id), ...research.market_evidence.map(row => row.id)]);
        validateThesisV2(item.thesis, { symbol: item.symbol, marketId: item.market_evidence[0].id, evidenceIds: known,
          sourceIndex: research.source_index, admittedAt: research.assessment.admitted_at_utc });
      } catch (cause) { fail(cause.message); }
      if (item.action === 'buy' && (item.thesis.status !== 'sufficient' || !['technical_flow_v2', 'news_event_v2'].includes(item.thesis.strategy)
          || item.time_stop_days === null || item.time_stop_days < 5 || item.time_stop_days > 7
          || item.target_weight > .2 + 1e-10 || item.risk_usd > item.reference_capital_usd * .008 + .01
          || !near(item.risk_usd, item.shares * (item.entry_price - item.stop_price))
          || Math.abs(item.target_weight - item.shares * item.entry_price / item.reference_capital_usd) > 1e-6)) fail('V3 buy must retain the sufficient thesis and frozen numeric risk plan');
    }
    if (item.action === 'watch' && (item.target_weight !== 0 || ['entry_price', 'stop_price', 'shares', 'risk_usd', 'time_stop_days'].some(key => item[key] !== null))) fail('Watch instructions require a zero target and no executable numeric plan');
  }
  unique(instructions, 'symbol', 'executionDecisions');
}

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
  const fields = ['schemaVersion', 'idempotencyKey', 'asOf', 'runId', 'receiptHash', 'dataAsOf', 'executionMode', 'modelMode', 'status', 'universeSize', 'summary', 'decisions'];
  const extras = ['candidate_research', 'opportunity_lifecycle', 'business_evaluation', 'portfolio_snapshot', 'execution_policy', 'workflow_phase', 'universe_coverage'];
  shape(record, record?.schemaVersion === 3 ? [...fields, 'model', 'executionDecisions', ...extras] : record?.schemaVersion === 2 ? [...fields, 'model', 'executionDecisions'] : fields, 'research');
  if (![1, 2, 3].includes(record.schemaVersion)) fail('Research schemaVersion must be 1, 2 or 3');
  if (Buffer.byteLength(canonical(record)) > (record.schemaVersion === 3 ? 4 : 1) * 1_048_576) fail('Research payload exceeds its versioned byte limit');
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
  if (record.schemaVersion >= 2) {
    if (record.schemaVersion === 3) validateResearchV3Fields(record);
    if (record.modelMode !== 'enabled' || record.status !== 'completed') fail('Only fully completed required models can produce execution instructions', 'MODEL_REQUIRED');
    if (record.schemaVersion === 3 && record.model?.research_contract !== 'tiger.short-term-research.v2') fail('Schema3 requires its six-stage v2 model contract', 'MODEL_REQUIRED');
    if (record.schemaVersion === 2 && record.model?.research_contract !== undefined) fail('Historical schema2 requires its original five-stage model contract', 'MODEL_REQUIRED');
    validateModelEvidence(record.model);
    validateExecutionDecisions(record.executionDecisions, record.decisions, record.asOf, record);
  }
  return record;
}


const PRIVATE_FIELDS = new Set(['content', 'raw', 'raw_response', 'model_rounds', 'model_output', 'reasoning_content', 'chain_of_thought', 'messages', 'api_key', 'apikey', 'authorization', 'password', 'credential', 'token', 'secret', 'source_account_state', 'cohort_inputs']);
export function validatePublicTree(value, depth = 0, counter = { n: 0 }) {
  if (++counter.n > 100000 || depth > 48 || value === undefined || typeof value === 'number' && !Number.isFinite(value)) fail('Public research value exceeds its bounds');
  if (value === null || typeof value === 'boolean' || typeof value === 'number') return;
  if (typeof value === 'string') { if (value.length > 100000 || /[\x00-\x08\x0b\x0c\x0e-\x1f]/u.test(value)) fail('Public research text is invalid'); return; }
  if (Array.isArray(value)) { if (value.length > 20000) fail('Public research array is too large'); for (const item of value) validatePublicTree(item, depth + 1, counter); return; }
  if (!value || typeof value !== 'object' || ![Object.prototype, null].includes(Object.getPrototypeOf(value))) fail('Public research must be ordinary JSON');
  for (const [key, child] of Object.entries(value)) { if (PRIVATE_FIELDS.has(key.toLowerCase())) fail('Private source or model material cannot be published'); validatePublicTree(child, depth + 1, counter); }
}
function validateResearchV3Fields(record) {
  try {
    if (record.execution_policy !== 'whitepaper-execution-v2' || !['daily', 'premarket', 'intraday'].includes(record.workflow_phase)) fail('Research execution policy or phase is invalid');
    const research = record.candidate_research;
    exact(research, ['schema', 'assessment', 'plan', 'theses', 'coverage', 'counters', 'coverage_summary', 'fact_evidence', 'market_evidence', 'source_index'], 'Candidate research');
    if (research.schema !== 'tiger.candidate-research.v2') fail('Candidate research version is invalid');
    if (!research.assessment || !validDate(research.assessment.run_date) || research.assessment.market_timezone !== 'America/New_York') fail('Frozen assessment date is missing');
    isoTime(research.assessment.admitted_at_utc);
    if (Date.parse(research.assessment.admitted_at_utc) > Date.parse(record.asOf)) fail('Research admission cannot follow completion');
    exact(research.plan, ['targets', 'requested_symbols', 'deferred', 'limits', 'plan_sha256'], 'Research plan');
    for (const key of ['targets', 'requested_symbols', 'deferred']) list(research.plan[key], `research.plan.${key}`, 500);
    text(research.plan.plan_sha256, 'research.plan.plan_sha256', 64, /^[a-f0-9]{64}$/);
    for (const key of ['theses', 'fact_evidence', 'market_evidence', 'source_index']) list(research[key], `research.${key}`, 5000);
    unique(research.source_index, 'id', 'research.source_index'); unique(research.fact_evidence, 'id', 'research.fact_evidence'); unique(research.market_evidence, 'id', 'research.market_evidence'); unique(research.theses, 'symbol', 'research.theses');
    for (const source of research.source_index) {
      exact(source, ['id', 'title', 'url', 'published_at', 'fetched_at', 'source', 'full_content_characters', 'presented_content_characters', 'content_complete', 'original_content_sha256', 'presented_content_sha256', 'search_source_id', 'target_symbols', 'presentations'], 'Source metadata');
      text(source.id, 'source.id', 100); text(source.title, 'source.title', 500); publicUrl(source.url); number(source.fetched_at, 'source.fetched_at');
      if (!['deepseek_search', 'deepseek_fetch'].includes(source.source)) fail('Source method is invalid');
      if (source.published_at !== null && !Number.isFinite(Date.parse(source.published_at))) fail('Observed publication date is invalid');
      for (const key of ['full_content_characters', 'presented_content_characters']) { number(source[key], `source.${key}`, 0, 100000000); if (!Number.isSafeInteger(source[key])) fail('Source coverage must be integral'); }
      if (source.presented_content_characters > source.full_content_characters || typeof source.content_complete !== 'boolean' || source.content_complete !== (source.presented_content_characters === source.full_content_characters)) fail('Source coverage does not reconcile');
      for (const key of ['original_content_sha256', 'presented_content_sha256']) text(source[key], `source.${key}`, 64, /^[a-f0-9]{64}$/);
      list(source.target_symbols, 'source.target_symbols', 500); list(source.presentations, 'source.presentations', 500);
      unique(source.presentations, 'symbol', 'source.presentations');
      for (const shown of source.presentations) {
        exact(shown, ['symbol', 'presented_content_characters', 'content_complete', 'presented_content_sha256'], 'Source presentation'); symbol(shown.symbol, 'source.presentation.symbol');
        number(shown.presented_content_characters, 'source.presentation.characters', 0, source.full_content_characters);
        if (!Number.isSafeInteger(shown.presented_content_characters) || typeof shown.content_complete !== 'boolean' || shown.content_complete !== (shown.presented_content_characters === source.full_content_characters)) fail('Per-symbol content coverage differs');
        text(shown.presented_content_sha256, 'source.presentation.hash', 64, /^[a-f0-9]{64}$/);
      }
      if (source.presentations.length && Math.max(...source.presentations.map(row => row.presented_content_characters)) !== source.presented_content_characters) fail('Top coverage must reflect an actual issuer presentation');
    }
    const evidenceIds = new Set([...research.source_index.map(row => row.id), ...research.fact_evidence.map(row => row.id), ...research.market_evidence.map(row => row.id)]);
    for (const thesis of research.theses) validateThesisV2(thesis, { symbol: thesis.symbol,
      marketId: research.market_evidence.find(row => row.symbol === thesis.symbol)?.id,
      evidenceIds, sourceIndex: research.source_index, admittedAt: research.assessment.admitted_at_utc });
    const lifecycle = record.opportunity_lifecycle;
    exact(lifecycle, ['schema', 'account_kind', 'revision', 'scope', 'opportunities', 'intents', 'events', 'cohort_outcomes'], 'Opportunity lifecycle');
    if (lifecycle.schema !== 'tiger.opportunity-ledger.v2' || lifecycle.account_kind !== 'paper' || !Number.isSafeInteger(lifecycle.revision) || lifecycle.revision < 0) fail('Public lifecycle must be a paper ledger');
    for (const key of ['opportunities', 'intents', 'events', 'cohort_outcomes']) list(lifecycle[key], `lifecycle.${key}`, 20000);
    const evaluation = record.business_evaluation;
    if (evaluation?.schema !== 'tiger.business-evaluation.v2' || evaluation.run_id !== record.runId || evaluation.account?.account_kind !== 'paper' || evaluation.account.profitability_established !== false) fail('Business measurements must remain bound paper results without an established-profit claim');
    isoTime(evaluation.as_of); if (Date.parse(evaluation.as_of) > Date.parse(record.asOf)) fail('Evaluation cannot use a future outcome');
    const portfolio = record.portfolio_snapshot;
    exact(portfolio, ['schema', 'known_at', 'nav', 'cash', 'gross', 'reserved_cash', 'reserved_risk', 'held_risk', 'group_risk', 'committed_symbols', 'held', 'reservations', 'state_hash'], 'Paper portfolio snapshot', ['frozen_at']);
    if (portfolio.schema !== 'tiger.portfolio-snapshot.v2' || !validDate(portfolio.known_at) || portfolio.known_at > record.asOf.slice(0, 10)) fail('Paper valuation date is invalid');
    for (const key of ['nav', 'cash', 'gross', 'reserved_cash', 'reserved_risk', 'held_risk']) number(portfolio[key], `portfolio.${key}`);
    if (portfolio.nav <= 0 || !near(portfolio.nav, portfolio.cash + portfolio.gross)) fail('Paper NAV must equal dated cash plus held value');
    text(portfolio.state_hash, 'portfolio.state_hash', 64, /^[a-f0-9]{64}$/);
    if (portfolio.frozen_at !== undefined) { isoTime(portfolio.frozen_at); if (Date.parse(portfolio.frozen_at) > Date.parse(record.asOf)) fail('Portfolio cannot be frozen in the future'); }
    validatePublicTree({ candidate_research: research, opportunity_lifecycle: lifecycle, business_evaluation: evaluation, portfolio_snapshot: portfolio, universe_coverage: record.universe_coverage });
  } catch (cause) { if (cause instanceof PnlError) throw cause; fail(cause.message); }
}
