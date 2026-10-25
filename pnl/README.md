# Tiger PnL public ledger

Tiger PnL means **Tiger Profit & Loss｜投资业绩与决策记录**. This independent website publishes genuine account snapshots and Tiger research decisions. It does not place orders or turn the existing paper account into a real account. An empty deployment displays `awaiting_first_record`; it never seeds illustrative performance.

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

Daily Tiger research can update while the real account snapshot remains unchanged. Submit research to `POST /api/v1/research` with the same Bearer token, JSON content type, idempotency rules and 1 MiB limit. Its independent durable chain is stored under `DATA_DIR/research/records`; account records remain under `DATA_DIR/records`. Query authenticated research receipts at `GET /api/v1/research/receipts/:idempotencyKey`. Publish with `node pnl/publish.mjs --research /private/path/verified-research.json`; the default account command is unchanged.

Required research payload fields (unknown fields, NAV, cash, positions and executions are rejected):

| Field | Meaning |
|---|---|
| `schemaVersion`, `idempotencyKey`, `asOf` | Version `1`, stable safe key, actual research completion/publication timestamp with timezone; increasing within research ledger. |
| `runId`, `receiptHash` | Controlled Tiger runner's independently verified run identity and original SHA-256 receipt hash; placeholders are rejected. A run is disclosed once; subsequent retries use its original key. |
| `dataAsOf` | Actual market data date (`YYYY-MM-DD`) or ISO timestamp with timezone, no later than `asOf`; stale market data remains visibly dated. |
| `executionMode` | Exactly `paper`, disclosing the controlled research workflow; never evidence of real execution. |
| `modelMode` | `disabled`, `enabled` or `passthrough`, taken from verified runner evidence; model absence/degradation must not be hidden. |
| `status` | Only `completed` or `completed_degraded`; failed or running research is rejected. |
| `universeSize` | Integer from 0 to 100,000, taken from the actual research scope. |
| `summary` | Nonempty plain text summary, up to 10,000 characters. |
| `decisions` | Same decision schema as account snapshots; every decision's `runId` must match this research `runId`. Recommendations are never fills. |

The public research shape is `{sequence,hash,asOf,runId,receiptHash,dataAsOf,executionMode,modelMode,status,universeSize,summary,decisions}`; idempotency keys are excluded. It is exposed at `dashboard.research.latest` and `.history`, even before the real account baseline exists. Its updates never manufacture daily NAV observations, holdings, trades or returns. Account `status` remains `awaiting_first_record` until a genuine account snapshot is submitted.

The generic research publisher validates the declared evidence fields and verifies the website's write receipt. The caller must first run the controlled runner's independent `verify`, then build this payload from that verified original receipt. A syntactically valid `receiptHash` alone does not establish a genuine Tiger run. If no new actual account evidence exists, publish research only and leave account `asOf` unchanged.

For local tests only, pass `--allow-local-http` with a loopback URL. This flag never permits plaintext HTTP to a remote host. Different AI clients use the same narrow publisher; they never receive SSH access or direct ledger filesystem access. Rotate the single secret in the protected service and runner environments together when needed; there is no browser admin password or complex account system.

Schedule the runner after each US trading day or at the desired periodic cadence. A schedule should publish only when genuine account evidence is available. If evidence, price data, Tiger verification or account reconciliation fails, skip publishing and report the reason privately; never carry a made-up daily row. The website itself does not imply that a calendar schedule has been created.

## Controlled research sync from Codex or another AI

`pnl/sync-research.mjs` completes the research-only loop through the existing **controlled** `scripts/tiger-agent.mjs`. It calls doctor and describe first, submits the four-field fixed-profile request when necessary, observes only its existing run identity, and requires successful independent receipt and verify operations before publishing. It never calls an unrestricted Python pipeline, brokers, actual orders or a free trading CLI.

Configure these protected runner environment values outside Git:

| Variable | Required value |
|---|---|
| `TIGER_AGENT_PYTHON` | Absolute path to the installed supported Python environment, validated by the controlled doctor. |
| `TIGER_AGENT_STATE_ROOT` | Absolute private directory used by the controlled Tiger runner. |
| `TIGER_AGENT_PERMISSION` | Exactly `paper`, explicitly selected by the operator's trusted startup configuration. |
| `TIGER_PNL_SYNC_STATE_DIR` | Absolute protected private directory outside the source tree for persistent request identity, run ID, research payload, evidence metadata and website receipt. |
| `TIGER_PNL_RESEARCH_PROFILE` | Defaults to `us-public-paper-v1`. `us-public-active-paper-v1` is an explicit research experiment; this release's evaluation does not qualify it for automatic promotion. The controlled describe must support the selected profile. |
| `TIGER_PNL_WRITE_TOKEN`, `TIGER_PNL_URL` | Same protected website credential and HTTPS origin used by the publisher. The token is never written into sync state or passed to Python's environment. |

Run `node pnl/sync-research.mjs`. The default identity date is the current date in `America/New_York`, and the default profile remains `us-public-paper-v1`. For explicit reproducible identity, run `node pnl/sync-research.mjs --date YYYY-MM-DD --profile us-public-paper-v1`. The active profile requires an explicit `--profile us-public-active-paper-v1` or trusted environment selection after independent evidence and acceptance pass. The identity date labels the scheduled job and is not a historical-market-data request. Profiles are a fixed reviewed whitelist; CLI flags cannot change Python, agent roots, permission, individual stages, providers or strategy parameters.

The runner request key is fixed as `tigerpnl-research-<profile>-<date>`; the publication key is `pnl-<profile>-<date>`. The sync state is written with mode `0600`, atomic replacement and fsync **before** a mutating submission. If a submission response is lost before a run ID is known, the next invocation uses the exact original four-field request and key to recover its identity. Once a run ID is known, it only queries that ID. Accepted/running is never reported as completed. Every invocation has at most a 50-second observation window; a remaining worker yields `status:pending`, `observationStopped:true`, `workerCancellationRequested:false`. Run the same command again to continue observation. A Codex schedule should resume the same date/profile until published or an actionable failure appears; it must not change the key to hide a failed run.

To import a genuinely completed existing run, use `node pnl/sync-research.mjs --date YYYY-MM-DD --profile us-public-paper-v1 --run-id run-<32-hex-digits>`. Import never submits run. Receipt and independent verification still execute through the controlled CLI, then the original `receipt.json` and `request.json` are read from that run's private evidence directory and checked byte-for-byte against verified hashes. The selected profile must match the original receipt and request. Imported identities remain bound to that original request.

Public `asOf` is the original receipt's sealed completion time; `dataAsOf` comes from the benchmark's actual last bar. `picks` become explicitly labeled research buy recommendations with their verified target fractions and cards; `watchlist` becomes watch observations at target weight zero. Paper NAV, cash, positions, pending orders and executions are never copied into a public account payload or sync state. Model mode, data coverage and degraded completion remain disclosed. The exact research payload is persisted before its HTTPS write, so a lost website response retries identical content and does not rerun research. Completed sync outputs only the run identity and verified website receipt. Future invocations of an already published identity return `already_published`.

The local PID lock rejects active observers and conservatively reclaims a provably dead owner with an inode recheck. Like the ledger's startup guard, this is not an atomic cross-process stale-lock mutex. Production Linux schedulers should wrap the observer with an OS-level lock (`flock`) and keep one observer per date/profile. On macOS, use one serialized Codex chat observer, retain the same date/profile identity and never run simultaneous manual observers. Failed observations retain the original run ID and request; damaged evidence or rejected publication fails closed. No periodic schedule is implied merely by installing the script.

The full historical comparison and its limitations are in [TIGER-ACTIVITY-EVALUATION.md](../docs/TIGER-ACTIVITY-EVALUATION.md). Both configurations lost money in the evaluated interval; neither a completed workflow nor a passing correctness test establishes improved future investment returns.

## Private backup and isolated restore verification

Run `node pnl/backup.mjs --data-dir /var/lib/tiger-pnl --destination /var/backups/tiger-pnl/<unique-name>`. Both paths must be absolute. The destination must not exist and must be outside the live source and repository, including through symlinks. Failed backups preserve the incomplete destination for protected inspection; retries use a new destination, never overwrite an earlier backup.

The source remains read-only while the website keeps appending. The tool captures each chain's current contiguous immutable filename prefix, copies only those accepted files, fsyncs the new copies, and excludes locks and uncommitted `.pending-*` files. The two chains are independent, so their prefixes can reflect slightly different instants. It then opens the copied account chain with `Ledger` and the copied research chain with `ResearchLedger`, verifies their actual schemas, order, valuation rules, hashes and chain links, closes both, and writes `backup-summary.json` only after successful isolated verification. No live restore or production file replacement is performed.

Successful stdout is a compact summary containing time, each chain's file count and head hash. It contains no private account contents, evidence references, attestors or credentials. Backup records remain private (`0700` directories, `0600` record files). The service operator must preserve those permissions and keep backups outside the public static tree. A systemd backup timer can generate a fresh UTC timestamped destination; a successful verified backup is a recoverable artifact, not permission to overwrite production.

## Verified implementation references

The implementation uses the official Node 24 [filesystem API](https://nodejs.org/docs/latest-v24.x/api/fs.html) for exclusive files, hard links and fsync, [crypto API](https://nodejs.org/docs/latest-v24.x/api/crypto.html#cryptotimingsafeequala-b) for hashed constant-time credential comparisons, and [HTTP API](https://nodejs.org/docs/latest-v24.x/api/http.html#serverrequesttimeout) for explicit request limits and timeouts. These references were checked during implementation; deployment must verify the actual runtime path and version.

Deployment, rollback, exact-domain TLS activation and operator checks are documented in [deploy/README.md](deploy/README.md).
