/** Fixed v2 contract shared by the thin client and the public publisher.
 * This validates sealed evidence; it never calls a model or authorizes a trade.
 */
export const UPGRADE_PROFILE = 'us-public-model-required-paper-v2';
export const PREMARKET_PROFILE = 'us-public-model-required-premarket-paper-v2';
export const INTRADAY_PROFILE = 'us-public-model-required-intraday-paper-v2';
export const UPGRADE_PROFILES = Object.freeze([UPGRADE_PROFILE, PREMARKET_PROFILE, INTRADAY_PROFILE]);
export const MODEL_STAGES_V2 = Object.freeze(['clean.llm_semantic', 'tech.sentiment', 'tech.risk', 'sector.narrative', 'research.candidates', 'decision.debate']);
const LIMITS = Object.freeze({ max_chat_calls: 40, max_tokens: 300000, max_search_calls: 60, max_fetch_calls: 20, max_remote_requests: 120, max_cost_usd: 1, deadline_seconds: 2400 });
const HASH = /^[a-f0-9]{64}$/;
const ID = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$/;
const reject = message => { throw new Error(message); };
const plain = value => value !== null && !Array.isArray(value) && typeof value === 'object' && [Object.prototype, null].includes(Object.getPrototypeOf(value));
export function exact(value, fields, label = 'Contract', optional = []) {
  if (!plain(value) || fields.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !fields.includes(key) && !optional.includes(key))) reject(`${label} has missing or unsupported fields`);
}
const finite = (value, min = 0, max = 1e15) => typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max;
const integer = (value, min = 0, max = 1e15) => Number.isSafeInteger(value) && value >= min && value <= max;
const hash = value => typeof value === 'string' && HASH.test(value) && !/^0+$/.test(value);
export function plainText(value, maximum = 2000) {
  if (typeof value !== 'string' || !value.trim() || value.length > maximum || /[<>\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/u.test(value)) reject('Contract text is invalid');
}
function ids(values, maximum = 1000) {
  if (!Array.isArray(values) || values.length > maximum || new Set(values).size !== values.length || values.some(value => typeof value !== 'string' || !ID.test(value))) reject('Evidence identity list is invalid');
}
export function isoTime(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value)) || !validDate(value.slice(0, 10))) reject('Observation timestamp is invalid');
}
export function validDate(value) {
  return typeof value === 'string' && !value.startsWith('0000-') && /^\d{4}-\d{2}-\d{2}$/.test(value)
    && Number.isFinite(Date.parse(`${value}T00:00:00Z`)) && new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value;
}
export function publicUrl(value) {
  let url;
  try { url = new URL(value); } catch { reject('Source URL is invalid'); }
  if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.hash
      || [...url.searchParams.keys()].some(key => /token|key|signature|authorization|credential|secret|password/i.test(key.replace(/[_-]/g, '')))) reject('Source URL must be public and credential free');
}
export function validateModelV2(model) {
  exact(model, ['provider', 'model', 'release', 'required', 'research_contract', 'response_ids', 'call_count', 'required_stages', 'stage_counts', 'search_count', 'fetch_count', 'search_mode', 'all_required_completed', 'budget'], 'V2 model');
  if (model.provider !== 'deepseek' || model.model !== 'deepseek-flash' || model.release !== 'DeepSeek-V4.1-Flash-1017' || model.required !== true || model.all_required_completed !== true || model.research_contract !== 'tiger.short-term-research.v2' || model.search_mode !== 'provider_native_search') reject('Exact completed required model is mandatory');
  if (!Array.isArray(model.required_stages) || model.required_stages.join('|') !== MODEL_STAGES_V2.join('|')) reject('All six ordered semantic stages are required');
  exact(model.stage_counts, MODEL_STAGES_V2, 'V2 stage counters');
  for (const count of Object.values(model.stage_counts)) if (!integer(count, 1, LIMITS.max_chat_calls)) reject('Stage count is invalid');
  if (!integer(model.call_count, 6, LIMITS.max_chat_calls) || Object.values(model.stage_counts).reduce((a, b) => a + b, 0) !== model.call_count || !integer(model.search_count, 14, LIMITS.max_search_calls) || !integer(model.fetch_count, 0, LIMITS.max_fetch_calls)) reject('Completed I/O counters are invalid');
  ids(model.response_ids, LIMITS.max_chat_calls);
  if (model.response_ids.length !== model.call_count) reject('Every chat needs a unique provider response');
  const budget = model.budget;
  exact(budget, ['schema', 'limits', 'pricing', 'counts', 'actual_tokens', 'reserved_tokens', 'estimated_charged_usd', 'estimated_reserved_usd', 'frozen', 'records'], 'Request budget');
  if (budget.schema !== 'tiger.request-budget.v2' || budget.frozen !== false || budget.reserved_tokens !== 0 || budget.estimated_reserved_usd !== 0) reject('Every paid request must have settled usage');
  exact(budget.limits, Object.keys(LIMITS), 'Request limits');
  if (Object.entries(LIMITS).some(([key, value]) => budget.limits[key] !== value)) reject('Request budget differs from the reviewed policy');
  const price = budget.pricing;
  exact(price, ['observed_date', 'source', 'model', 'token_upper_usd_per_million', 'search_upper_usd_per_request', 'fetch_upper_usd_per_request', 'basis', 'invoice'], 'Budget pricing');
  if (price.observed_date !== '2026-10-27' || price.source !== 'https://api-docs.deepseek.com/quick_start/pricing/' || price.model !== 'deepseek-flash' || price.token_upper_usd_per_million !== 1.2 || price.search_upper_usd_per_request !== .00075 || price.fetch_upper_usd_per_request !== .00075 || price.invoice !== false) reject('Unreviewed budget pricing');
  plainText(price.basis, 500);
  exact(budget.counts, ['chat', 'search', 'fetch'], 'Budget counts');
  if (budget.counts.chat !== model.call_count || budget.counts.search !== model.search_count || budget.counts.fetch !== model.fetch_count) reject('Budget and model counts disagree');
  if (!Array.isArray(budget.records) || budget.records.length !== model.call_count + model.search_count + model.fetch_count || budget.records.length > LIMITS.max_remote_requests) reject('Budget record coverage differs');
  let actual = 0, charged = 0; const counts = { chat: 0, search: 0, fetch: 0 }, calls = new Set();
  for (const [index, record] of budget.records.entries()) {
    exact(record, ['reservation', 'status', 'usage'], 'Budget record');
    const r = record.reservation;
    exact(r, ['schema', 'call_id', 'kind', 'sequence', 'request_sha256', 'input_token_upper_bound', 'output_token_upper_bound', 'token_upper_bound', 'estimated_cost_upper_usd', 'counts_before', 'actual_tokens_before', 'reserved_tokens_before'], 'Budget reservation');
    if (record.status !== 'settled' || r.schema !== 'tiger.request-budget-reservation.v2' || !/^[a-f0-9]{32}$/.test(r.call_id) || calls.has(r.call_id) || !hash(r.request_sha256) || !Object.hasOwn(counts, r.kind) || r.sequence !== index + 1) reject('Unsettled or duplicate request reservation');
    calls.add(r.call_id);
    exact(r.counts_before, ['chat', 'search', 'fetch'], 'Admission counters');
    if (Object.keys(counts).some(kind => r.counts_before[kind] !== counts[kind]) || r.actual_tokens_before !== actual || r.reserved_tokens_before !== 0) reject('Admission ledger does not reconcile');
    for (const key of ['input_token_upper_bound', 'output_token_upper_bound', 'token_upper_bound']) if (!integer(r[key], 0, LIMITS.max_tokens)) reject('Token reservation invalid');
    if (r.token_upper_bound !== r.input_token_upper_bound + r.output_token_upper_bound) reject('Token bounds do not reconcile');
    const reservationCost = r.kind === 'chat' ? r.token_upper_bound * price.token_upper_usd_per_million / 1e6 : price[`${r.kind}_upper_usd_per_request`];
    if (!finite(r.estimated_cost_upper_usd, 0, 1) || Math.abs(r.estimated_cost_upper_usd - reservationCost) > 1e-12) reject('Reservation cost is invalid');
    if (r.kind === 'chat') {
      exact(record.usage, ['prompt_tokens', 'completion_tokens', 'total_tokens'], 'Actual usage', ['prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'prompt_tokens_details', 'completion_tokens_details']);
      const u = record.usage;
      if (!integer(u.prompt_tokens, 0, r.input_token_upper_bound) || !integer(u.completion_tokens, 0, r.output_token_upper_bound) || u.total_tokens !== u.prompt_tokens + u.completion_tokens) reject('Actual usage exceeds admission');
      for (const key of ['prompt_cache_hit_tokens', 'prompt_cache_miss_tokens']) if (key in u && !integer(u[key], 0, u.prompt_tokens)) reject('Cache usage invalid');
      for (const [parent, child, maximum] of [['prompt_tokens_details', 'cached_tokens', u.prompt_tokens], ['completion_tokens_details', 'reasoning_tokens', u.completion_tokens]]) if (parent in u) { exact(u[parent], [child], 'Detailed usage'); if (!integer(u[parent][child], 0, maximum)) reject('Detailed usage invalid'); }
      actual += u.total_tokens; charged += u.total_tokens * price.token_upper_usd_per_million / 1e6;
    } else {
      if (record.usage !== null || r.token_upper_bound !== 0) reject('Native I/O cannot invent token usage');
      charged += reservationCost;
    }
    counts[r.kind]++;
  }
  if (Object.keys(counts).some(kind => counts[kind] !== budget.counts[kind]) || actual !== budget.actual_tokens || actual > LIMITS.max_tokens || !finite(budget.estimated_charged_usd, 0, LIMITS.max_cost_usd) || Math.abs(charged - budget.estimated_charged_usd) > 1e-12) reject('Settled token or cost totals disagree');
  return model;
}
export function validateThesisV2(thesis, { symbol, marketId = null, evidenceIds = null, sourceIndex = [], admittedAt = null } = {}) {
  exact(thesis, ['schema_version', 'symbol', 'strategy', 'status', 'issuer_name', 'issuer_identity_evidence_ids', 'five_part', 'claims', 'event_risk', 'entry_triggers', 'falsification', 'risk_notes', 'gaps', 'followup'], 'Thesis');
  if (thesis.schema_version !== 2 || thesis.symbol !== symbol || !['technical_flow_v2', 'news_event_v2'].includes(thesis.strategy) || !['sufficient', 'insufficient', 'contradictory'].includes(thesis.status)) reject('Thesis identity or status is invalid');
  if (thesis.issuer_name !== null) plainText(thesis.issuer_name, 200);
  ids(thesis.issuer_identity_evidence_ids);
  for (const id of thesis.issuer_identity_evidence_ids) {
    const source = sourceIndex.find(row => row.id === id);
    if (!source || !Array.isArray(source.target_symbols) || !source.target_symbols.includes(symbol)) reject('Issuer identity evidence must belong to the assessed symbol');
  }
  const references = refs => { ids(refs, 12); if (evidenceIds && refs.some(id => !evidenceIds.has(id))) reject('Thesis references unknown evidence'); };
  exact(thesis.five_part, ['market', 'sector', 'chain', 'stock', 'plan'], 'Five part thesis');
  for (const paragraph of Object.values(thesis.five_part)) { exact(paragraph, ['text', 'evidence_ids'], 'Thesis paragraph'); plainText(paragraph.text, 1000); references(paragraph.evidence_ids); if (!paragraph.evidence_ids.length) reject('Each business reason needs evidence'); }
  if (marketId && !thesis.five_part.stock.evidence_ids.includes(marketId)) reject('Stock thesis must cite its own frozen market snapshot');
  if (!Array.isArray(thesis.claims) || thesis.claims.length > 8) reject('Claim count is invalid');
  const claimIds = new Set(); let primaryCurrent = false;
  for (const claim of thesis.claims) {
    exact(claim, ['claim_id', 'statement', 'source_id', 'quote', 'origin_id', 'evidence_kind', 'time_role'], 'Thesis claim', ['date_source_id']);
    if (!ID.test(claim.claim_id) || claimIds.has(claim.claim_id)) reject('Duplicate claim identity'); claimIds.add(claim.claim_id);
    plainText(claim.statement, 700); plainText(claim.quote, 1000);
    references([claim.source_id]); if (claim.origin_id !== null) references([claim.origin_id]);
    if (claim.date_source_id !== undefined && claim.date_source_id !== null) references([claim.date_source_id]);
    if (!['primary', 'media', 'commentary', 'unknown'].includes(claim.evidence_kind) || !['current', 'background', 'unknown'].includes(claim.time_role)) reject('Claim source/time role invalid');
    if (claim.time_role === 'current' && sourceIndex.length) {
      const source = sourceIndex.find(row => row.id === claim.source_id);
      const when = claim.date_source_id ? sourceIndex.find(row => row.id === claim.date_source_id) : source;
      if (!source || !when || (claim.date_source_id && (source.search_source_id !== when.id || source.url !== when.url)) || !when.published_at || !Number.isFinite(Date.parse(when.published_at)) || !admittedAt || Date.parse(when.published_at) > Date.parse(admittedAt) || Date.parse(admittedAt) - Date.parse(when.published_at) > 7 * 86400000) reject('Current claim needs observed timely publication evidence');
      const presentation = source.presentations?.find(row => row.symbol === symbol);
      if (claim.evidence_kind === 'primary' && claim.origin_id === source.id && presentation?.content_complete === true && thesis.issuer_identity_evidence_ids.includes(source.id)) primaryCurrent = true;
    }
  }
  exact(thesis.event_risk, ['status', 'text', 'source_ids'], 'Event risk');
  if (!['checked', 'unknown'].includes(thesis.event_risk.status)) reject('Event risk status invalid'); plainText(thesis.event_risk.text, 1000); references(thesis.event_risk.source_ids);
  for (const [key, high, max] of [['entry_triggers', 5, 600], ['falsification', 5, 600], ['risk_notes', 5, 800], ['gaps', 8, 600]]) {
    const values = thesis[key]; if (!Array.isArray(values) || values.length > high || (key !== 'gaps' && !values.length) || new Set(values).size !== values.length) reject('Trigger, falsification or risk disclosure invalid');
    for (const value of values) plainText(value, max);
  }
  if (thesis.status !== 'sufficient' && !thesis.gaps.length) reject('Insufficient thesis must disclose gaps');
  exact(thesis.followup, ['needed', 'question', 'fetch_source_id'], 'Follow-up');
  if (typeof thesis.followup.needed !== 'boolean') reject('Follow-up status invalid');
  if (thesis.followup.question !== null) plainText(thesis.followup.question, 40);
  if (thesis.followup.fetch_source_id !== null) references([thesis.followup.fetch_source_id]);
  if (!thesis.followup.needed && (thesis.followup.question !== null || thesis.followup.fetch_source_id !== null)) reject('Unneeded follow-up cannot carry an action');
  if (thesis.strategy === 'news_event_v2' && thesis.status === 'sufficient' && (!primaryCurrent || !thesis.issuer_name || !thesis.issuer_identity_evidence_ids.length)) reject('Event thesis requires timely complete primary issuer evidence');
  return thesis;
}
