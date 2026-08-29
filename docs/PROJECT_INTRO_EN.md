# Tiger Trading — Project Introduction

> In one sentence: a fully automated paper-trading system with the Caishen AI
> trading system as its kernel and WorkLoom IM as its governance shell —
> covering US / CN / HK markets, running 24 hours.
> **No prediction. Process, discipline, audit.**

**Current stage: paper trading only. Not investment advice; no returns promised.**

## 1. Origin: From Caishen to Tiger Trading

The project started as the [Caishen AI trading system](CAISHEN_README.md): a
multi-agent trading system that strictly follows the
[White Paper: AI Short-Term US Equity Trading (1–15 Day Swing Edition)](../reports/AI短线美股交易白皮书_20260730.pdf)
— a six-layer decision stack of MRS (market resonance) → SHS (sector heat) →
ICS (industry-chain cycle) → TSS (entry scoring) → risk gates, plus a signal
journal, no-lookahead backtesting, rolling WFA tuning, and DSR multiple-testing
correction — forming a closed loop of "every signal settled, win rate verifiable,
parameters iterable".

Caishen solved "how to trade", but not "how to govern": who is accountable for
each trading action, whether the white-paper red lines have a second machine
enforcement, and how review conclusions are preserved. Two research efforts
shaped Tiger Trading:

### Research 1: Industry Benchmarks ([research/01_benchmark](../research/01_benchmark.html))

A deep dive on Bridgewater AIA and 8 other benchmarks. Three directly adopted ideas:
- **Bull/Bear debate evidence layer** (Bridgewater AIA debate protocol): Bull,
  Bear, and Coordinator debate gray-zone picks and produce a "sixth section" of
  evidence on each trade card — report-only, never mutates scores (shipped in S2);
- **Statistical calibration layer + sample threshold**: calibration from signal
  scores to realized outcomes; below 50 samples only Wilson-interval statements
  are published, with a standing "calibration samples accumulating (n/50)"
  disclosure (shipped in S2);
- **Three-column friction accounting**: paper-trading ledger split into
  gross/net/friction columns, with ADV-tiered slippage (shipped in S2).

### Research 2: Frontline Investment-Team Know-How ([research/02_frontline](../research/02_frontline.html))

The conclusion: **data foundation and governance traceability matter more than
model cleverness**:
- Data hygiene (multi-source cross-validation, credibility tiers, freshness
  gates) must precede any scoring (shipped in S3);
- Every action must be accountable: five-element events
  (Who × Context × Object × Decision × RuleImpact) with a hash chain;
- Review must be institutionalized: review the process, never the luck; the six
  violation rules are always flagged (Appendix D standard).

Both studies merged into the [Upgrade Plan v3](../research/UPGRADE_PLAN_v3.md)
(Caishen-centric edition, confirmed): **zero kernel rewrite; the shell governs**.

## 2. Architecture: Kernel + Shell + Role Bundle

```
┌──────────── WorkLoom IM Governance Shell (governance/, TypeScript) ───────────┐
│ Five-element event store (hash-chain audit) · Fence engine (white-paper       │
│ thresholds → block-level baseline) · Quest orchestration · night-shift watch  │
│ · Approval cards · Organizational memory · site (accountability UI)           │
│ ┌────────── Caishen Trading Kernel (repo root, Python, fully preserved) ────┐ │
│ │ 21-step pipeline (red-line roll-call; a missing step is an incident)      │ │
│ │ Six-layer stack: L0 universe scan → L1 MRS → L2 SHS → L2b ICS → L3 TSS    │ │
│ │ → L4 risk · SearchHub 6 intelligence sources · 7-ring quote fallback      │ │
│ │ chain · zero-baseline discipline (every run starts from scratch)          │ │
│ │ Signal journal → calibration layer → WFA+DSR iteration · paper-trading    │ │
│ │ engine ($100,000) · Review loop (Zhuge team): daily attribution / weekly  │ │
│ │ checkup / monthly WFA proposal + three-gesture approval                   │ │
│ └───────────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────────┘
```

### Trading Kernel (repo root, Python)

- **Single source of truth**: all weights/thresholds/tiers live in
  `trading_system/config.py`;
- **21-step pipeline**: the registry in `trading_system/redline/` is the
  contract; ExecutionTracer rolls call on every step and a missing step raises
  RedlineViolation (a systemic incident); LLM steps are irreversible (passthrough
  fallback only — no rule-based substitute branch exists anywhere);
- **Zero-baseline discipline**: every run purges leftovers from the previous run
  (search cache / full-universe list / backtest frame cache); only accounting
  ledgers are whitelisted (journal / sim_portfolio / calibration samples /
  governance event log);
- **Engineering tests**: 252 pytest cases, all green, including static audits
  (guarding against score-mutation paths and decision-chain drift).

### Governance Shell (governance/, TypeScript, WorkLoom IM base snapshot)

- **Governance only, never decisions**: produces no trading scores; it turns
  every trading action into an accountable event;
- **Five-element events + hash chain**: kernel→shell bridge
  (`trading_system/governance_bridge.py`, S5) records every pipeline step, L4
  gate decision, paper-trading fill, and compliance hit; the SHA-256 chain is
  verifiable (tamper-evident);
- **Three-layer fences**: baseline layer (block, immutable; R-T1~R-T15 generated
  from config via `scripts/gen_fences.py`) / customer patch layer (review;
  tightening only) / strategy snapshot layer.

### Industry Role Bundle (governance/bundles/trading/)

Six assembly slots (per the WorkLoom bundles spec): profile schemas, object &
stage enums (8 object types; 5 stages: observation / paper / small-live /
scaling / drawdown-control), toolsets, fence packs, agent presets (the 20
Caishen agents re-wrapped + debate group / calibration officer / data-quality
officer / compliance officer + the 4-member review team), and workbench UI.

## 3. Three-Market Support (S4: same framework, swapped inputs)

One six-layer decision stack covers three markets; all differences are
parameterized in `config.py` and `trading_system/markets/`:

| Market | Benchmark set | Sector proxies | Special rules |
|---|---|---|---|
| US | SPY / TNX / VIX / VIX9D | 12 sector ETFs | Current behavior (honest failure on missing benchmarks) |
| CN (A-shares) | CSI 300 / CN 10Y / 50ETF iVIX | CSI 800 industry indices | Price limits ±10%/±20%/±5%; built-in trading calendar |
| HK | HSI / US 10Y / VHSI | Hang Seng industry indices | VCM cooling-off symbol set; half-day calendar |

Discipline: D1 framework unchanged (threshold structure intact; inputs swapped
only); D2 unattainable dimensions are recorded as missing and renormalized —
never fabricated; D7 new markets start in the light-probe channel and graduate
only after 50 settled trades with a passing DSR.

## 4. Review & Optimization Loop (S6: the Zhuge team)

Four digital employees on the review line (`trading_system/review/`):

| Role | Module | Responsibility |
|---|---|---|
| Attribution analyst | `attribution.py` | Daily per-trade attribution (signal source layer / entry quality / exit type / market environment snapshot) + automatic detection of the six violations (Appendix D: entering when MRS forbids / off-mainline trades / chasing under no-chase conditions / not de-risking before earnings / not executing a triggered stop / no protection at ≥2R) — every hit is flagged and counted |
| Statistician | `weekly.py` | Weekly checkup: cumulative / last-20 / last-50 win rate, expectancy R, per-template breakdown, R distribution, calibration sample (n/50) linkage; three-tier verdict (on-track / watch / alert; thresholds in config) |
| Strategy optimizer | `monthly.py` | Monthly WFA proposal: insignificant DSR → automatic reject (D7 iteration honesty); significant → pending_review; **proposes only, never activates** |
| Zhuge (review host) | `chief.py` | Orchestrates daily/weekly/monthly cadences, produces the review memo `reports/复盘_<date>.md`, and emits it as a governance five-element event |

Approval flow (aligned with WorkLoom's three gestures):
- `python3 main.py --review-list` lists proposals;
- `--review-approve <id>` → status approved, writes `tuned_params.json`
  (effective_from = next day), reusing the `--use-tuned` discipline: not loaded
  by default, explicitly enabled, and the activation is disclosed in the daily
  report + a governance event; **next-day effectiveness** is machine-enforced by
  `apply_tuned_params`;
- `--review-reject <id> --reason "..."` → the reason is mandatory and flows back
  into organizational memory;
- In the paper-trading stage, approvals never block the pipeline (they only
  govern whether a parameter proposal takes effect).

Red line: review artifacts are accounting-ledgers (D5) — report layer and
governance events only, never decision inputs (locked by pytest static audits).
Review the process, never the luck.

## 5. Compliance & Boundaries

- **Paper trading only**: the system places no real orders; the paper account
  ($100,000 initial, T+1 open-price fills, no lookahead) is the vehicle for
  public verification (gains and losses reported truthfully, no cosmetic
  framing, D8);
- **Not investment advice**: this is a technical research and simulation project;
- **Data boundaries**: free market-data sources carry delays (annotated in
  reports); the production chain never falls back to synthetic data;
- **Preconditions for live trading**: separate integration with a licensed
  broker and compliance with each market's regulations (US SEC 15c3-5, CN
  program-trading filing, HK SFC), with review-level fences switched to blocking
  approvals.

## 6. Roadmap

| Stage | Content | Status |
|---|---|---|
| S1 | WorkLoom base imported into `governance/`; kernel runs unchanged | ✅ Done |
| S2 | Debate layer / calibration layer / three-column friction | ✅ Done |
| S3 | Source credibility tiers / cross-validation / tiered TTL | ✅ Done |
| S4 | Three-market `markets/` abstraction (US/CN/HK) | ✅ Done |
| S5 | bundles/trading role bundle, gen_fences.py, governance_bridge.py five-element events | ✅ Done |
| S6 | Review loop (Zhuge team) + bilingual documentation | ✅ This release |
| Next | Three-month public paper-trading validation (weekly/monthly review cadence, full proposal-flow demonstration) | In progress |
| Future | Live trading: licensed broker integration, blocking review-level approvals, regulatory filings | Not started |

---

Related documents: [Root README](../README.md) · [README_EN](../README_EN.md) ·
[中文版本](PROJECT_INTRO.md) · [Kernel documentation](CAISHEN_README.md) ·
[Governance design](GOVERNANCE.md) · [Upgrade Plan v3](../research/UPGRADE_PLAN_v3.md)
