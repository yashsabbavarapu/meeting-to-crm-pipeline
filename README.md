# Meeting-to-CRM Pipeline

Turn sales-call transcripts into CRM updates that are cited, reviewable, and safe to re-run.

Sales reps spend their day in calls (Gong, Chorus, Fireflies record all of them), but the CRM only learns what a rep remembers to type in afterwards. So the CRM rots: stale amounts, missing contacts, next steps from three calls ago. This pipeline closes that loop. It reads the transcript, extracts the facts a rep would have entered, and writes them to the CRM. Low-confidence or destructive changes wait for a human first.

The extraction is deliberately simple (rules, not a model). The engineering around it is what this project is about, because that's what decides whether AI survives a real customer deployment:

- **Every fact cites its source.** Each one carries the verbatim quote, speaker, and timestamp it came from, plus a confidence score that explains itself.
- **Nothing destructive happens without a human.** Overwriting a field, moving a deal backwards, or touching a closed-won deal goes to a review queue.
- **Re-running is always safe.** Running the same transcript twice never creates duplicate contacts, tasks, or audit rows.
- **Everything is audited.** Each proposed change is logged with its outcome (applied, no-op, queued, superseded, approved, rejected) and its citation.
- **Quality is measured.** An eval harness scores precision and recall per field against hand-labeled transcripts.
- **Per-customer config, not code.** Thresholds, deal stages, CRM field names, and competitor lists live in one YAML file.

Python 3.11+, stdlib only (zero runtime dependencies), `mypy --strict` clean.

## Architecture

```
                         config.yaml (per customer: thresholds, stages, field_map, competitors)
                              │
 transcript.txt               ▼
 (Gong/Chorus export) ─► ingest ─► extract ──► gate ──┬─ conf ≥ threshold ─────────────┐
                         │          │           │      │                                │
                         ▼          ▼           │      └─ conf < threshold ─► review    │
                    utterances   facts with     │                             queue     │
                    (JSON)       quote + ts +   │                            (JSONL)    │
                                 confidence +   │                         approve/reject│
                                 reasons        │                               │       │
                                                ▼                               ▼       ▼
                                             sync ◄─────────────────────────────────────┘
                                               │  destructive? (overwrite, stage regression,
                                               │  protected stage) ──► back to review queue
                                               │  older than the field's last writer? ──► superseded
                                               ▼
                                     CRMAdapter interface
                                     ├── SQLiteCRM (reference: accounts, contacts, deals,
                                     │              activities, applied_facts, audit_log)
                                     └── Salesforce / HubSpot (v2, same interface)
```

| Module | Responsibility |
|---|---|
| `ingest.py` | Parse `[HH:MM:SS] Speaker: text` transcripts with `# key: value` headers. Messy lines warn, never crash. |
| `extract.py` | Rule-based extraction of 8 fact kinds, each with a confidence built from named components. |
| `review.py` | Confidence gating and the JSONL review queue (approve, reject, escalate, supersede). |
| `sync.py` | All write policy: idempotency, overwrite protection, protected stages, provenance, audit. |
| `crm/base.py` | `CRMAdapter` abstract interface. Adapters are dumb; policy stays in `sync.py`. |
| `crm/sqlite.py` | Reference CRM with uniqueness constraints that back up the idempotency logic. |
| `evals.py` | Per-field precision, recall, F1, and attribute accuracy against labeled fixtures. |
| `yamlish.py` | A strict YAML subset parser so config stays YAML with no dependencies. |

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install pytest mypy   # dev only; runtime is stdlib
.venv/bin/pip install -e .                                    # optional: installs `meeting-crm`
```

Without installing, use `PYTHONPATH=src python -m meeting_crm` everywhere you see `meeting-crm` below.

## Commands

| Command | What it does |
|---|---|
| `meeting-crm ingest FILE...` | Parse transcripts into `.meeting-crm/transcripts/` |
| `meeting-crm extract [ID...] [-v]` | Extract and gate facts (all ingested calls by default); `-v` shows citations and score reasons |
| `meeting-crm review list [--all]` | Show facts waiting for a human, with reason, citation, and score breakdown |
| `meeting-crm review approve ID [--note ...] [--by ...]` | Approve a queued fact (an id prefix is enough) |
| `meeting-crm review reject ID [--note ...] [--by ...]` | Reject a queued fact; it will never be applied |
| `meeting-crm sync` | Apply auto-approved and human-approved facts to the CRM, oldest call first |
| `meeting-crm show` | Print the current deal, contacts, and activities |
| `meeting-crm audit [--limit N] [-v]` | Print the audit log; `-v` adds citations and reasons |
| `meeting-crm eval [--min-confidence X] [-v]` | Score extraction against `fixtures/labels/`; `-v` lists every miss |

Global options go before the subcommand: `--config config.yaml`, `--workdir .meeting-crm`, `--db PATH`.

## Demo walkthrough

Three synthetic calls between Northwind Software (the seller) and Acme Logistics: a discovery call, a post-demo follow-up, and a pricing negotiation where a competitor comes up.

**1. Ingest and extract.**

```bash
meeting-crm ingest fixtures/transcripts/*.txt
meeting-crm extract
```

```
gong-58211  Discovery call: Acme Logistics x Northwind  (10 facts: 8 auto, 2 to review)
  AUTO   0.85  a052844f  action_item: owner=Sam Ortiz, task=send over our SOC 2 report...
  REVIEW 0.70  4d332a3d  action_item: owner=Dana Whitfield, task=share a week of histo...
  AUTO   0.85  0b7b8b4c  contact: name=Dana Whitfield, role=VP of Operations
  REVIEW 0.65  0a4747ae  budget: amount=120000
  AUTO   0.85  a4c0fd49  timeline: target=2027-Q1
  ...
```

**2. See why two facts need a human.**

```bash
meeting-crm review list
```

```
  0a4747ae  [pending] low_confidence  conf 0.65  budget: amount=120000
      reason: confidence 0.65 below budget threshold 0.80
      cite:   gong-58211 [00:01:44] Dana Whitfield: "We have roughly $120k budgeted for routing
              tooling this fiscal year, maybe a bit more if the ROI case is strong."
      why: customer states an amount next to 'budgeted' (+0.85); hedged language: 'roughly' (-0.20)
```

**3. Sync.** High-confidence facts apply. The third call's go-live target ("January") would overwrite the discovery call's "Q1", so it's held for review instead:

```bash
meeting-crm sync
```

```
applied 27 facts (24 CRM writes)
already applied (skipped, idempotent): 0
waiting for review (low confidence): 2
held for review (destructive change): 1
```

**4. Review.** Approve the hedged budget and the request to Dana. Then sync again:

```bash
meeting-crm review approve 0a47 --by reviewer --note "Dana confirmed in email"
meeting-crm review approve 4d33 --by reviewer
meeting-crm sync
```

```
applied 1 facts (1 CRM writes)
already applied (skipped, idempotent): 27
held for review (destructive change): 1
superseded by a newer call (logged, not written): 1
```

The $120k budget was approved late, but the CRM already holds $110k from the newer negotiation call. Field provenance catches this, so the stale value is logged as `superseded` and not written. Without that check, clearing last week's queue would quietly roll back this week's data.

**5. Approve the overwrite, sync, and sync again to prove idempotency.**

```bash
meeting-crm review approve 96cf --by reviewer --note "Dana confirmed January go-live"
meeting-crm sync     # applied 1 facts
meeting-crm sync     # applied 0 facts (0 CRM writes), already applied: 30
meeting-crm show
```

```
DEAL  Acme Logistics - Route Optimization  (account: Acme Logistics)
  stage        negotiation
  amount       110000
  close_target 2027-01
  competitors  Routewise;FleetPilot
  risks        security_review;legal_review;integration_risk
  next_step    2026-10-16: Let's meet on October 16 to finalize terms

CONTACTS
  Dana Whitfield    VP of Operations        -
  Marcus Lee        IT Security Lead        marcus.lee@acmelogistics.com
  Elena Ruiz        Procurement Manager     elena.ruiz@acmelogistics.com
```

Plus 9 tasks with owners and resolved due dates, and one meeting activity per call. `integration_risk` is a false positive: Elena said a *competitor* doesn't integrate. The eval catches it (below), and it's the kind of error that motivates v2.

**6. Audit trail.**

```bash
meeting-crm audit -v --limit 4
```

```
...  superseded approved:reviewer   deal.amount '110000' -> '120000'
    cite: gong-58211 [00:01:44] Dana Whitfield: "We have roughly $120k budgeted for ..."
    note: amount was set by a newer call (gong-59302, 2026-10-06)
...  applied    approved:reviewer   deal.close_target '2027-Q1' -> '2027-01'
    cite: gong-59302 [00:03:20] Dana Whitfield: "We still want everything live across all depots by January."
```

To reset the demo: `rm -rf .meeting-crm`.

## How confidence works

Each rule starts from a base score and adds or subtracts named components. All of them show up in `reasons`, so a reviewer can see exactly why the system believed something. Examples:

| Signal | Effect |
|---|---|
| First-person commitment with an action verb ("I'll send...") | base 0.80 |
| Direct request to a named participant ("Dana, can you...") | base 0.70 |
| Collective "we'll..." (owner is ambiguous) | base 0.55 |
| Due date or meeting date stated | +0.05 |
| Hedged language ("roughly", "maybe", "probably") | -0.20 |
| Risk phrase negated nearby ("no legal review needed") | -0.40 |
| Competitor named by the customer vs by our own rep | 0.90 vs 0.65 |
| Contact email local part matches the contact's name | +0.10 |
| Same fact mentioned again in the call | +0.05, every citation kept |

Speaker side matters: budgets, timelines, risks, and contacts only count when the *customer* says them. A rep quoting $138,000 is a price, not the customer's budget.

## Sync guarantees

1. **Idempotent.** Facts have deterministic ids (`sha256(call_id, kind, key)`). A fact is applied at most once (`applied_facts` table), and each write is also a no-op if the CRM already holds the value. Unique constraints on contacts and activities back this up. Re-running `extract` keeps human decisions.
2. **Human approval for destructive changes.** These go to the queue: overwriting a non-empty field with a different value, moving the stage backwards, any change to a deal in a `protected_stages` stage (closed_won, closed_lost). Fields that are expected to change every call (`next_step`) are listed in `freely_updated_fields`. Appending to multi-select fields (competitors, risks) is not destructive.
3. **Approval is scoped.** Approving a low-confidence fact does not authorize an overwrite. If it turns out to be destructive at sync, it goes back to pending with the new reason.
4. **Newer calls win.** A fact from an older call never overwrites a field last written from a newer call. It's logged as `superseded`.
5. **Atomic per fact.** A fact's writes, audit rows, and its applied marker commit in one transaction. If the CRM write fails, nothing is recorded, and a retry applies it exactly once.

## Evals

```bash
meeting-crm eval -v
```

```
field           tp  fp  fn   prec  recall     f1
action_item      9   0   0   1.00    1.00   1.00
contact          4   0   0   1.00    1.00   1.00
budget           2   0   0   1.00    1.00   1.00
timeline         2   0   0   1.00    1.00   1.00
competitor       2   0   0   1.00    1.00   1.00
risk             4   1   1   0.80    0.80   0.80
next_step        3   0   0   1.00    1.00   1.00
stage            3   0   0   1.00    1.00   1.00
----------------------------------------------
OVERALL         29   1   1   0.97    0.97   0.97

Attribute accuracy on matched facts:
  action_item.due     6/7
  contact.email       2/2
  contact.role        4/4

Errors:
  01_discovery: action_item dana whitfield/00:03:45 due: expected '2026-09-09', got '(none)'
  03_pricing_negotiation: false positive risk: category=integration_risk @ 00:02:05
  03_pricing_negotiation: missed risk budget_constraint
```

The errors are real and instructive. The rules can't tell whose integration problem it is. They miss "that is above what we can do" because it's a paraphrase, not a lexicon phrase. And the due date sits in Dana's reply, not in the request. At `--min-confidence 0.75` (roughly what auto-applies), overall precision is 0.96 and recall 0.90. The gap is what the review queue is for.

Labels live in `fixtures/labels/*.json`. A predicted fact matches a label on its kind's key fields (an action item matches on owner plus timestamp; a contact on name). `tests/test_evals.py` asserts a floor of 0.9 precision and recall on the fixtures, so a rule change that degrades quality fails CI.

**Read these numbers with skepticism:** the fixtures and the rules were written by the same author, so this measures regression, not real-world accuracy. Real transcripts will score lower.

## Configuration

See `config.yaml` (fully commented). Highlights:

```yaml
thresholds: {default: 0.75, contact: 0.70, budget: 0.80, risk: 0.75}
stages:
  - name: negotiation
    signals: ["pricing proposal", "discount", "order form", "contract term"]
protected_stages: [closed_won, closed_lost]
field_map: {budget: amount, timeline: close_target, next_step: next_step}
competitors: [Routewise, FleetPilot, Optimo]
sync: {review_overwrites: true, freely_updated_fields: [next_step]}
```

Config is validated on load: unknown fact kinds, thresholds outside 0 to 1, protected stages that aren't defined, and `field_map` targets the CRM adapter doesn't support all fail with a clear message.

## Transcript format

```
# call_id: gong-58211                         (stable id; falls back to a content hash)
# title: Discovery call
# date: 2026-09-08                            (required; relative dates resolve against it)
# account: Acme Logistics                     (required)
# deal: Acme Logistics - Route Optimization   (optional; default from config)
# internal: Sam Ortiz, Priya Nair             (optional; seller-side speakers, merged with config)
[00:00:04] Sam Ortiz: Thanks for making the time today.
continuation lines are appended to the previous utterance
```

## Checks

```bash
.venv/bin/python -m pytest              # 58 tests
.venv/bin/python -m mypy --strict src   # clean
```

CI (`.github/workflows/ci.yml`) runs both on Python 3.11 and 3.12, plus a CLI smoke run over the fixtures.

## What v1 does NOT do

- **No LLM.** Extraction is regex plus rules. It's explainable and deterministic, but it misses paraphrases and can't resolve context ("they" in a competitor sentence). The eval shows exactly where.
- **Synthetic fixtures.** Three hand-written calls, labeled by the same person who wrote the rules. Not real customer data, and too small to give a real accuracy estimate.
- **SQLite, not Salesforce.** The `CRMAdapter` interface is built for real CRMs, but only the SQLite reference adapter exists. No OAuth, rate limits, or API retries.
- **No transcript connectors.** Input is a cleaned-up text format, not the Gong or Chorus API.
- **One deal per call.** The account and deal come from transcript headers. There's no entity resolution against existing CRM records (no fuzzy name matching).
- **Review is a CLI over a JSONL file.** Single user, no locking, no notifications.
- **Dates are heuristic.** "Friday" means the next Friday after the call. "May" is ignored as a month because it's usually a verb.

## v2 roadmap

- **LLM extraction behind the same interface.** `Extractor.extract(transcript) -> list[Fact]` stays the contract. The model returns facts with quotes, and quotes are verified verbatim against the transcript before acceptance. The rule-based extractor stays as a baseline in the same eval harness, so the two are compared on the same numbers.
- **Real adapters.** Salesforce (Opportunity, Contact, Task; `field_map` to `Amount`, `CloseDate`, `NextStep`) and HubSpot implementations of `CRMAdapter`. Gong and Chorus ingestion via their APIs.
- **Slack review UI.** Queue items posted with the citation and approve or reject buttons. Decisions write to the same audit log.
- **Entity resolution.** Match spoken names and accounts to existing CRM records before creating new ones.
- **Bigger evals.** More fixtures, labels from someone other than the rule author, and per-customer eval sets tracked over time.
