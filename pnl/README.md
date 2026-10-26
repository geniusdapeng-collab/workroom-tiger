# Tiger PnL public ledger

Tiger PnL means **Tiger Profit & Loss｜投资业绩与决策记录**. This independent website publishes genuine account snapshots and model-generated Tiger trading instructions for a human execution team. The team executes the instructions at its brokers; actual fills, positions and NAV are recorded from execution reports. The website itself has no broker order endpoint. An empty deployment displays `awaiting_first_record`; it never seeds illustrative performance.

## Runtime and private storage

No npm packages or build step are required. Run with the existing Node 24 runtime. The service listens on `127.0.0.1:4348`; put HTTPS Nginx in front of it. `pnl/public` contains the static frontend. Configure these values in the server's protected service environment, outside Git:

| Variable | Meaning |
|---|---|
| `TIGER_PNL_WRITE_TOKEN` | Shared Bearer secret, 32–512 characters, no whitespace. Generate a random secret in the deployment's protected secret store. |
| `TIGER_PNL_DATA_DIR` | Required absolute private directory outside the source tree; only the service OS account should have access. |
| `PORT` | Optional port, defaults to `4348`; the bind address is always loopback. |

Start with `node pnl/server.mjs`. Run tests with `node --test pnl/test/*.test.mjs`. Do not put credentials or real snapshot files in the repository, shell history, public assets or test fixtures. Public read endpoints require no credentials. The write token belongs only in an AI runner's secret environment and the service's protected environment.

The files `DATA_DIR/records/000000000001.json`, etc. form a sequential SHA-256 chain. Writes are serialized in a single process, written into an exclusive temporary file, fsynced, atomically hard-linked to a previously nonexistent final filename, and directory-fsynced before returning a receipt. Existing accepted files are never replaced. A write error stops further writes until recovery. Startup reads and validates every accepted record, sequence, payload hash and chain hash. A process owner lock prevents concurrent ordinary starts; only a provably dead local PID's stale lock is reclaimed. The lock is a startup guard, not a cross-process atomic mutex: simultaneous stale-lock recovery can race. Production must run one systemd service instance under an outer OS lock (`/usr/bin/flock`) and use a local filesystem, not NFS or shared multi-host storage. Data paths are resolved through symlinks and rejected if their real target is inside the source or public static directory.

An interrupted write may have committed before the network response was received. Always retry with **the same key and exact same payload**. A leftover uncommitted `.pending-*` file is removed on verified startup. A committed record survives a restart. An unreadable process lock or damaged chain fails closed and needs operator review. Back up the entire data directory regularly; hashes detect edits to chain contents but do not prove broker authenticity or prevent an administrator from replacing the entire ledger or truncating its tail. Archive receipts independently if independent tamper evidence is needed.

## Public API contract

`GET /healthz` → `{ "status": "ok", "records": 0, "research": 0, "ledger": "verified" }`. A failed durable write in either ledger changes health status to `503/unavailable`.

`GET /api/public/dashboard` always returns this shape:

```json
{
  "schemaVersion": 1,
  "status": "awaiting_first_record",
  "generatedAt": "ISO timestamp",
  "baseline": null,
  "latest": null,
  "metrics": null,
  "history": [],
  "records": [],
  "research": { "latest": null, "history": [] }
}
```

After the first genuine record, `status` becomes `active`, with these exact field definitions:

| Field | Structure |
|---|---|
| `baseline` | `{date: asOf-of-first-record, nav: first-account-nav}` |
| `latest` | Public snapshot, described below |
| `metrics` | `{returnPct, drawdownPct, unrealizedPnl, positionCount, recordCount, netExternalFlow}` |
| `history` | All actual observations in chronological order: `[{asOf, nav, returnPct, drawdownPct}]` |
| `records` | Up to 20 latest public snapshots, newest first |
| `research` | Independent research `{latest:null\|public-research,history:[public-research]}`; all actual research entries newest first, does not affect account `status`, baseline or history |

A public snapshot is `{sequence, hash, asOf, account:{currency,nav,cash}, positions, trades, decisions}`. Each position is `{symbol,name,quantity,avgCost,price,marketValue,weight,unrealizedPnl}`. `weight` is a fraction (0–1); `unrealizedPnl` is USD. Trades and decisions use the exact submission fields listed below. **Account label, idempotency key, provenance, evidence hash, source reference and attestor are excluded from every public projection.** Public strings are plain text and must be rendered with DOM `textContent`, never inserted as HTML.

`GET /api/public/records?limit=20&offset=0` → `{schemaVersion:1,total,limit,offset,records:[public-snapshots]}`, newest first. `limit` is 1–100; `offset` is a nonnegative safe integer. Unknown or repeated pagination parameters return `400`.

## Return methodology

The first record starts the public account: its return and drawdown are **0%**, with no invented earlier history. For each subsequent snapshot, multiply the previous unit index by `(current NAV − net external flow) / previous NAV`. Deposits are positive and withdrawals negative. The submitted flow is assumed to occur at the end of the observation interval. Report `returnPct = (index − 1) × 100`; `drawdownPct = (index / highest-observed-index − 1) × 100`, expressed as a nonpositive percentage. The metric is the current drawdown, not maximum historical drawdown. This end-of-period flow convention is approximate when substantial cash movements happen during the interval; submit an extra snapshot immediately before and after a cash movement for better attribution.

The first record must have `netExternalFlow:0`, because its NAV establishes the starting capital. Cumulative `metrics.netExternalFlow` sums subsequent flows only. NAV must equal cash plus long-only position market values; account label is fixed after baseline. Leveraged, short, multi-currency and derivatives accounts are deliberately outside this schema. No annualized return, backfilled simulation curve or benchmark comparison is fabricated. Multiple genuine observations per day are supported and are retained as actual observations.

## Authenticated submission

`POST /api/v1/records` with `Authorization: Bearer <secret>` and `Content-Type: application/json`. The JSON body below describes the schema, **not a real submission**; never publish placeholders. All fields are required and unknown fields are rejected at every object level.

| Field | Required structure / constraints |
|---|---|
| `schemaVersion` | `1` |
| `idempotencyKey` | Unique 8–128 characters matching `[A-Za-z0-9][A-Za-z0-9_.:-]*`; persist it before submitting. |
| `asOf` | Actual account valuation time in ISO format with explicit timezone, strictly later than the last accepted record; future times over five minutes are rejected. |
| `account` | `{label:string,currency:"USD",nav:number,cash:number}`; private stable account alias, never a broker account number; positive NAV and nonnegative cash. |
| `positions` | `[{symbol,name,quantity,avgCost,price,marketValue}]`, up to 500 unique symbols; quantity/price/value positive, average cost nonnegative; value must equal quantity × price within rounding tolerance. Empty means genuinely all cash. |
| `trades` | `[{id,executedAt,symbol,side:"buy"\|"sell",quantity,price,fees}]`, up to 2,000. Only newly disclosed actual executions since the previous snapshot; IDs may never repeat. Empty means no new confirmed executions. |
| `decisions` | `[{id,symbol,action:"buy"\|"sell"\|"hold"\|"watch",targetWeight,thesis,decidedAt,runId}]`, up to 500, target fractions 0–1 with sum ≤1. Research recommendations are distinct from actual executions and holdings. |
| `netExternalFlow` | Signed USD deposits minus withdrawals during this snapshot's interval; first record must be zero. |
| `provenance` | `{kind:"broker_statement"\|"operator_attested",evidenceSha256,sourceRef,attestedBy}`; real evidence hash (64 lower-case hexadecimal characters), private evidence reference and responsible attestor. No placeholders, paper, mock, simulation or synthetic sources. |

An optional `Idempotency-Key` header must match the body's key. Newly committed submission returns HTTP `201` with `{receipt:{schemaVersion,idempotencyKey,sequence,asOf,acceptedAt,hash,previousHash,payloadHash,replayed:false}}`. Retrying the same payload returns `200` with `replayed:true`; reusing a key for changed content returns `409/IDEMPOTENCY_CONFLICT`. Corrections require a **new later snapshot** and a new key; there is no delete or overwrite API.

`GET /api/v1/receipts/:idempotencyKey` with the same Bearer authentication returns `{receipt}` or `404/RECEIPT_NOT_FOUND`. This endpoint returns no account or evidence details.

`operator_attested` accepts an explicit truthful account-owner/operator confirmation, including a genuinely all-cash starting account. Hash the exact confirmation text as evidence and keep its reference private. The API validates the evidence form and declared source; it cannot independently verify whether a token holder's assertion is truthful. The operator must inspect real records before attesting. A successful Tiger paper workflow is research evidence only and **must never be used as the source of real NAV, real holdings or real executions**. Research decisions may refer to its verified `runId`, while actual account facts come from real evidence.

Errors use `{error:{code,message}}`. Expected codes include `UNAUTHORIZED` (401), `JSON_REQUIRED` / `ENCODING_NOT_SUPPORTED` (415), `BODY_TOO_LARGE` (413), `INVALID_JSON` (400), `INVALID_RECORD` / `REAL_EVIDENCE_REQUIRED` / `BASELINE_FLOW_MUST_BE_ZERO` / `INVALID_EXTERNAL_FLOW` / `TRADE_OUTSIDE_PERIOD` (422), `IDEMPOTENCY_CONFLICT` / `AS_OF_NOT_INCREASING` / `ACCOUNT_MISMATCH` / `DUPLICATE_TRADE` (409), and `LEDGER_UNAVAILABLE` / `LEDGER_WRITE_FAILED` (503). Bodies are limited to 1 MiB; header/request/socket timeouts are explicit. No request body, authentication header or token is logged.

## AI publisher workflow

1. Run Tiger via its controlled agent runner. Obtain its independently verified receipt and use its verified research decisions only.
2. Obtain genuine account-owner confirmation or broker/operator evidence of NAV, cash, actual positions and newly executed trades. Preserve evidence privately and calculate its SHA-256. Do not infer a fill from a recommendation.
3. Prepare a complete JSON snapshot in a private path outside the repository, with one stable idempotency key. Include verified research in `decisions`; keep actual holdings and executions evidence-based.
4. The AI runner receives `TIGER_PNL_WRITE_TOKEN` from its secret environment and optionally `TIGER_PNL_URL` (defaults to `https://tigerpnl.com`). Run `node pnl/publish.mjs /private/path/real-snapshot.json`.
5. The CLI submits via HTTPS with redirects disabled, fetches the authenticated receipt, and checks it against the local payload hash. It prints only the receipt. Network failure is not proof that submission failed; recover by querying/retrying the original key and payload.

## Independent periodic research

Research updates independently of real account records. New `POST /api/v1/research` submissions require **schemaVersion 3**, a completed independently verified v2 required-model run and the same Bearer/idempotency rules. Research v3 is bounded to 4 MiB; actual account writes and historical research schemas remain limited to 1 MiB. The research chain is `DATA_DIR/research/records`, separate from `DATA_DIR/records`. Query private write receipts at `GET /api/v1/research/receipts/:idempotencyKey`. Publish with `node pnl/publish.mjs --research /private/path/verified-research.json`.

Historical research v1/v2 keeps its original bytes, hashes, disclosed mode and exact-key retries. New legacy publications fail `MODEL_REQUIRED`; accepting an old exact retry does not append a new record or rewrite history.

| Field | V3 contract |
|---|---|
| `schemaVersion`, `idempotencyKey`, `asOf` | Version `3`, persisted safe key, original sealed completion timestamp, strictly increasing within this chain. |
| `runId`, `receiptHash`, `dataAsOf` | Independently verified run and exact receipt hash; actual benchmark last-bar date. One publication per run. |
| `executionMode`, `modelMode`, `status` | Exactly `paper`, `enabled`, `completed`. Model failure or degraded output cannot become new successful instructions. |
| `universeSize`, `summary`, `decisions` | Actual scan count and model-written action/reasons. Every decision belongs to this run. |
| `model` | Fixed DeepSeek alias/release observation, six ordered required stages and real response IDs/counters, Search/Fetch counters and fully settled `tiger.request-budget.v2`. |
| `executionDecisions` | Existing human-readable buy/watch fields plus complete structured `thesis`; numeric entry/stop/size/weight/risk remains the approved plan. |
| `candidate_research` | Selected/deferred candidate and holding coverage, five-part theses, fact/market references, bounded research counters and source metadata; no article bodies or model call text. |
| `opportunity_lifecycle` | Paper-only current-run plus all active opportunities and related intents/events/outcomes; explicit scope counts identify omitted historical records. Full historical ledger stays private. |
| `business_evaluation`, `portfolio_snapshot` | Maturity-aware paper measurements and current dated paper NAV/cash/holdings/reservations; no established-profit claim. |
| `execution_policy`, `workflow_phase`, `universe_coverage` | `whitepaper-execution-v2`; daily/premarket/intraday; observed listing/scan/selected/held coverage and actual limitations. |

Each instruction has one own-stock `{id,symbol,as_of,close,series_sha256}` market snapshot. The runtime hashes the complete frozen series into `md_<symbol>_<YYYYMMDD>_<hash-prefix>`. The public server validates identity/date/numeric shape; private independent verification reconstructs the series before publication. A dated close is a reference, never a fill or an executable live quote.

Each v2 thesis retains market, sector, industry chain, stock and plan reasons, source-bound quoted claims, entry triggers, falsification, risk notes and unresolved gaps. A sufficient `technical_flow_v2` may have no company news; sufficient `news_event_v2` needs an original issuer source observed within seven days of frozen admission and actually presented completely. Fetch time is not publication time. Unknown date/origin stays unknown; different hosts are not evidence of independent reporting.

News registries contain at most sixteen exact `{id,title,url,published_at,fetched_at,method,content_sha256}` entries, including supporting identity/date/origin references. `source_index` adds full/presented character counts, complete/partial flags and exact per-symbol hashes of the longest prefix actually delivered across research rounds. Article bodies stay private. The model sees at most 24000 characters per source and 60000 per three-issuer batch, with fair issuer budgets and actual requested Fetch/supplemental evidence first. The decision cannot expand an earlier partial delivery into a full article. Unknown/nonfinite numerical facts are never replaced with a neutral score.

Buy facts must retain the approved 5–7-day plan, positive entry/stop/size/risk, stop below entry, weight≤20% and risk≤current reference NAV×0.008. Watch has zero target and exactly null entry/stop/shares/risk/time-stop fields. The lifecycle separately tracks holdings and paper exits even with zero buys. A quote hit requests requalification; `INTENT_PENDING` still requires fresh quote/current risk checks. Paper holdings, intentions, simulated fills and counterfactual outcomes never update real account NAV or executions.

The UI shows the current run and all active opportunities with explicit counts, current paper cash/NAV/reservations and per-decision reference capital separately. Unmatured hit/recall metrics are unknown, not 0%; same-day repeated research does not establish returns. Candidate counterfactual outcomes cannot be summed into a portfolio return. The genuine account observation and immutable account chain remain separate.

The generic Bearer publisher validates the declared public contract and website receipt. It cannot independently retrieve private model artifacts; a valid-looking SHA alone is not model proof. The controlled sync checks both `receipt` and independent `verify`, exact request/receipt/summary hashes and the verified frozen source tree before projection. Model text is preserved.

## Controlled decision sync from Codex or another AI

`pnl/sync-research.mjs` uses the full checkout's controlled CLI and calls doctor, describe, run/status, receipt and independent verify. Default profile is `us-public-model-required-paper-v2`; the other allowed new profiles are `us-public-model-required-premarket-paper-v2` and `us-public-model-required-intraday-paper-v2`. All use the same independently initialized v2 paper account across phases. Legacy model-required sync may only recover an already persisted old publication payload; it cannot import or start a fresh legacy publication.

Keep the following external trusted configuration outside Git:

| Variable | Required value |
|---|---|
| `TIGER_AGENT_PYTHON` | Absolute supported interpreter path, validated by doctor. |
| `TIGER_AGENT_STATE_ROOT` | Absolute private v2 runner state root, separate from old v1; reuse across daily/premarket/intraday. |
| `TIGER_AGENT_PERMISSION` | Exactly `paper`, chosen in trusted startup configuration. |
| `TIGER_PNL_SYNC_STATE_DIR` | Absolute private directory for persistent request, run, verified payload and website receipt. |
| `TIGER_PNL_RESEARCH_PROFILE` | One of the three v2 profiles; omitted means daily v2. |
| Four `LLM_*` variables | Fixed API configuration from [AGENT-CLIENTS.md](../docs/AGENT-CLIENTS.md), key from protected environment only. |
| `TIGER_PNL_WRITE_TOKEN`, `TIGER_PNL_URL` | Protected website token and HTTPS origin; website credentials never pass to Python. |

```bash
# Daily or premarket: choose the actual phase, not an alternative retry identity.
node pnl/sync-research.mjs --date YYYY-MM-DD --profile us-public-model-required-paper-v2
node pnl/sync-research.mjs --date YYYY-MM-DD --profile us-public-model-required-premarket-paper-v2 --slot 0830
# A genuinely scheduled 10:30 New York intraday observation.
node pnl/sync-research.mjs --date YYYY-MM-DD --profile us-public-model-required-intraday-paper-v2 --slot 1030
```

Omitted date is the current New York date; it labels identity and does not request historical bars. Optional `--slot HHmm` is a four-digit actual New York scheduling label, allowed only on v2; intraday accepts `0930` through `1559`. Actual admission UTC and market date remain frozen independently, so a slot cannot manufacture an execution time. No future schedule is created by running this script.

Identity is `<profile>-<date>[-<HHmm>]`: run key `tigerpnl-research-<identity>`, publish key `pnl-<identity>`, state `<identity>.json`. Omitting slot retains one identity per profile/day. State is atomic, fsynced, mode 0600 and persisted before each mutation. A missing acceptance response recovers exactly the original four-field request/key; a known run never changes. Each observer waits at most 50 seconds; `pending` means its accepted worker continues. Resume with the identical date/profile/slot. Preserve terminal failures and their run IDs; never switch slots or keys to hide failure.

Add `--run-id run-<32-hex-digits>` to import a completed v2 run without submitting another. Original profile/request/receipt, exact summary and independent source verification must agree. Lost website responses retry the exact saved payload. `already_published` requires the matching persistent receipt metadata. The public `asOf` remains the original seal time and `dataAsOf` the actual benchmark date, never the observation retry time.

Shared task limits are 40 chat calls, 300000 actual plus reserved tokens, 60 Search, 20 Fetch, 120 remote requests, a conservative 1 USD estimate and 2400 seconds, admitted before I/O. Unknown paid outcomes freeze the task; there is no model/provider/rule fallback. Six semantic stages, dynamic Search/Fetch counts and all reservations/usage must reconcile. API alias mapping and upper-rate observation are frozen, not a billing guarantee; [official pricing](https://api-docs.deepseek.com/quick_start/pricing/) and [Search & Fetch](https://api-docs.deepseek.com/guides/search/) were checked 2026-10-27.

The local PID observer lock conservatively reclaims a provably dead owner with inode checks; it is not a distributed mutex. Linux schedules should wrap observers in OS `flock`; macOS uses one serialized chat observer. One account permits one business writer. Actual market schedules, holidays, bounded cadence and notification rules belong to the configured scheduler, not this script. Full implementation/phase contracts are in [TIGER-STRICT-WORKFLOW.md](../docs/TIGER-STRICT-WORKFLOW.md).

## Private backup and isolated restore verification

Run `node pnl/backup.mjs --data-dir /var/lib/tiger-pnl --destination /var/backups/tiger-pnl/<unique-name>`. Both paths must be absolute. The destination must not exist and must be outside the live source and repository, including through symlinks. Failed backups preserve the incomplete destination for protected inspection; retries use a new destination, never overwrite an earlier backup.

The source remains read-only while the website keeps appending. The tool captures each chain's current contiguous immutable filename prefix, copies only those accepted files, fsyncs the new copies, and excludes locks and uncommitted `.pending-*` files. The two chains are independent, so their prefixes can reflect slightly different instants. It then opens the copied account chain with `Ledger` and the copied research chain with `ResearchLedger`, verifies their actual schemas, order, valuation rules, hashes and chain links, closes both, and writes `backup-summary.json` only after successful isolated verification. No live restore or production file replacement is performed.

Successful stdout is a compact summary containing time, each chain's file count and head hash. It contains no private account contents, evidence references, attestors or credentials. Backup records remain private (`0700` directories, `0600` record files). The service operator must preserve those permissions and keep backups outside the public static tree. A systemd backup timer can generate a fresh UTC timestamped destination; a successful verified backup is a recoverable artifact, not permission to overwrite production.

## Verified implementation references

The implementation uses the official Node 24 [filesystem API](https://nodejs.org/docs/latest-v24.x/api/fs.html) for exclusive files, hard links and fsync, [crypto API](https://nodejs.org/docs/latest-v24.x/api/crypto.html#cryptotimingsafeequala-b) for hashed constant-time credential comparisons, and [HTTP API](https://nodejs.org/docs/latest-v24.x/api/http.html#serverrequesttimeout) for explicit request limits and timeouts. These references were checked during implementation; deployment must verify the actual runtime path and version.

Deployment, rollback, exact-domain TLS activation and operator checks are documented in [deploy/README.md](deploy/README.md).


## Public trading whitepaper

The unchanged historical PDF is served at `/whitepaper/tiger-trading-20260730.pdf` with `application/pdf`, exact Content-Length and existing static path/security boundaries. It is the original 2026-07 document (29 pages), SHA-256 `85f5abbd26b1f76c765255ce83ba73af88282e58b480887f2464e8f6e872dc67`, 4,104,128 bytes. Include it in the pinned `pnl/` release and verify source, archive and HTTPS download bytes. GET and HEAD work without the API being available. Its older model-passthrough and simulation descriptions do not weaken the current required-model execution contract. The page labels the document's original date and provides native read/download links.
