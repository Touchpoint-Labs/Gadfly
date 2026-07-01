# Gadfly — spec

## Goal

AI coding agents silently make thousands of consequential decisions the user never agreed to, and they accrete into something unintended and unauditable. **Gadfly is a Socratic supervision layer that sits in the agent's live tool-call loop, replaces the human-in-the-loop role to the degree the user dials, and catches every consequential decision the moment it's made — grounding it in the spec, surfacing it to the user, or at minimum logging it. Nothing unjustified goes unlogged.**

This is only possible *in* the loop: post-hoc review sees decisions already buried in a diff; static rule files can't anticipate novel ones. The human is raised in altitude, not removed — they engage through surfaced questions, the decisions ledger, and async edits, not keystrokes.

## The actors

- **The Builder** — the unchanged host coding agent (v1: Claude Code). Does all the work; maintains `codemap.md`. Gadfly never writes code.
- **The Architecture Supervisor** — a Socratic visionary loyal to *the user's* vision, never its own taste. Reads code "as a language" — conceptually, to grasp what is being done and why. Two voices, weighted by the dial: *question* (draw out, expose the unjustified assumption) and *assert* (state the vision as the user's proxy and redirect). Catches drift from the spec (letter and spirit), inconsistency with the realized structure and prior decisions, and **undiscussed consequential decisions** — the central class. Ignores micro-issues.
- **The Code Supervisor** — a logic skeptic: real bugs, edge cases, intent-violations within the code, hallucinated APIs. Never judges architecture or whether code should exist — the clean separation of concerns is what keeps the two from conflicting. May lazily read surrounding code on suspicion, under a hard tool budget.
- **The Human** — engages via answers to surfaced questions, the decisions ledger, spec edits, and async code edits. Always asked before irreversible actions.

Both supervisors **observe but never act**: read-only, isolated contexts (never merged — the anti-bias premise), stateless processes whose only output is a decision plus an optional terse note. State flows through files, not sessions.

## Memory — five files of the supervised project

| File | Owner | Role |
|---|---|---|
| `spec.md` | Human | The ideal. The standard the architect enforces against. AI adds only human-accepted macro decisions. |
| `claude.md` | Human | Enforced project rules. AI adds only principles the human has stated. Any existing CLAUDE.md is ingested and enforced. |
| `codemap.md` | Builder | Brief, descriptive **current state** — not a standard; read labeled "may itself reflect drift." |
| `decisions.md` | AI (architect) | The living record of the project's load-bearing decisions — not a log. Lean entries: what · why · scope (file + symbol anchors) · status; non-active entries collapse to one-line tombstones. Maintained by the architect via verdict ops. |
| `memory.md` | AI | Cross-project personal style and calibration — what the supervisors have learned about this user. |

Trust order: `spec > claude.md > codemap.md > decisions.md > memory.md`. Files loaded in full are capped: `memory.md` (AI-owned) self-compacts on overflow; `spec.md`/`claude.md` (human-owned) overflow by surfacing a proposed condensed version for the human's approval — never a silent rewrite. `decisions.md` is the exception: uncapped on disk, bounded by its injected slice — the most recent entries (~12) plus any whose scope files overlap the change being reviewed.

**Promotion is explicit acceptance.** A decision enters `spec.md` only when the human was asked and said yes. Auto-decided choices stay in `decisions.md`, never silently promoted. Superseded decisions are marked, never enforced stale.

## The intervention model

Review happens **before execution** — the unit the builder submits (a single tool call or a parallel batch) is judged at the gate, pre-execution, as one coherent whole with the builder's stated intent.

- **Deterministic routing (free, no model):** reads, searches, and web fetches pass silently. Builder edits to the managed memory files — `spec.md`, `claude.md`, `decisions.md` — are **denied outright** (no model): they are maintained by Gadfly and the human; the builder states the change in conversation instead. Other docs and notebooks are skipped; test edits get the code supervisor only. Every other file edit goes to **both** supervisors — architecture review of edits is the point of the project and is not pre-filtered away.
- **Commands:** a routine fast-path (read-only programs, including safe compositions of them) passes instantly; everything else goes to a cheap safety triage, which escalates the flagged ones to the architect — who allows, blocks, or asks the user, judged in project context. Commands are a trajectory risk, not a bug risk: they get the architect, not the code reviewer.
- **Parallel batches** are reviewed as one unit by one review, producing one verdict per action — never N myopic, redundant reviews.
- **The gate interface:** agree (silent — the default on a good build), disagree (+ a terse note: a sharp question or sharp objection, never an essay), surface an undiscussed decision (log or ask, per the dial), ask (always, for irreversible actions). Prefer allow-with-note over deny for non-critical findings; reserve deny for what must not land.
- A supervisor that is genuinely unsure does not block on the doubt — but never silently waves a consequential choice through either: it questions, decides-and-logs, or surfaces, per the dial. Unsure-and-consequential is always handled and recorded.

## The autonomy dial

One threshold, not modes: it controls only **how often undiscussed decisions surface to the user**.

- **Autonomous:** decide on the user's behalf, grounded in the spec's intent; log everything; surface almost never.
- **Balanced:** decide and log the small; surface the genuinely consequential.
- **Collaborative:** surface most conceptual/macro undiscussed decisions in real time.

Across the whole range, accountability is never removed — at full auto it is *batched* into the decisions ledger for later review. Irreversible operations ask the user regardless of dial. Surfacing v1 = a question relayed into the chat; the build pauses on that action until answered; the answer flows into `spec.md` or `decisions.md`.

## The decisions ledger

`decisions.md` is a **living document the architect owns**, not an append-only log. The bar for entry is high: the few load-bearing decisions a new engineer would need — a data model, a contract, a dependency, a cross-cutting convention, a deliberate spec deviation. Implementation mechanics, code-reviewer-lane findings, and easily-reversed choices never enter.

- **Mechanism:** the architect states **ops** in its verdict — `add` / `revise` / `retire` / `delete` — and the harness applies them; supervisors stay read-only. The discipline of when to emit what (add only when an allowing verdict settles something; never on a deny or ask) is the architect's own, held by its prompt — the harness applies what the supervisor states without second-guessing it.
- **Anchoring:** a decision's `scope` is file paths plus optional symbols (function/class). The file is the retrieval key — the injected slice is recent entries plus those whose scope overlaps the files being changed. The symbol is the staleness signal: at injection, a deterministic check annotates entries whose anchor disappeared (`[anchor missing]`); the architect — never the detector — decides whether that means renamed (revise), removed (retire/delete), or unclear (leave).
- **Supersession:** an `add` names the entries it replaces in `supersedes`; the harness flips them deterministically. `retire` keeps a one-line tombstone (what + reason — history is cheap); `delete` removes mis-logged noise outright.
- **Reconciliation is lazy:** there is no curator. The architect only ever sees a slice, and the slice is exactly what it can fix — contradictions between aged-out entries don't pollute context, and an area coming back to life pulls its old decisions back in for reconciliation when they matter again.
- **Promotion:** `human_accepted` — the user visibly decided — promotes the decision into `spec.md`. Everything else stays in the ledger.

## Combination & escalation

Both supervisors run as separate parallel calls; agree → silent allow; any disagreement → that supervisor's note, **disagreement wins**; both deny → both notes. A high rate of denials across different actions is the system working, not an escalation. True deadlock — the same action retried past a cap — surfaces to the human with both sides. LLM review errors retry (transient only, bounded); on persistent failure (rare — the model endpoint is down) the gate **steps aside and logs** — it emits no decision, handing the action back to the host's own permission flow, rather than an explicit allow that would vouch for unreviewed work. This is uniform, dangerous commands included: in the host's default mode, stepping aside on a non-allowlisted command means the human is asked natively — failing closed to the human without a second error policy. (Accepted residual: under permissive host settings — broad allowlists, bypass modes — an unreviewed flagged command passes on the host's say-so.) Deterministic routing fails closed.

## Feedback & calibration

The edit-ledger (every AI edit recorded post-execution) makes authorship detection free: anything in the tree not in the ledger is human. Signals: async human edits/reverts (reconciled at session start), spec edits, and answers to surfaced questions. Extraction is event-triggered and selective — most edits imply nothing; a repeated correction becomes a compact rule in `memory.md`/`claude.md` scope. Only active rules are injected, never raw history. Calibration is optional polish: the system runs with zero learning and degrades gracefully.

## Pre-build

The same Socratic agent in a **midwife** voice: at session start it checks the spec exists and is detailed enough to enforce, and asks targeted gap questions. It interrogates with the supervisor's own lens — "is this concrete enough to defend later?"

## Architecture constraints

- **Agent-agnostic in:** one adapter per agent is the only code that touches a native format; the core sees a normalized event and returns a verdict.
- **LLM-agnostic out:** supervisors call a provider-neutral client; models are config (defaults: Opus-class architect, Sonnet-class code, Haiku-class triage); v1 rides the user's existing Claude Code access.
- **Pure core:** `review(unit) → verdicts` assumes nothing about process lifetime or caller; per-process in v1, daemon later only if triggers demand it.
- **Core owns state:** the unified per-session file (conversation + gates) is the single normalized record; supervisors read slices of it; the core never parses a native transcript.
- **Fast common path:** deterministic routing, prompt caching (context ordered stable → dynamic), and a cheap triage model keep the loop affordable; don't wake the architect on what isn't architectural.

## Non-goals (v1)

- No post-hoc tester/QA "practitioner" — gatekeeper, not QA. (Seam stays open.)
- No taskmaster: Gadfly never forces a session to continue; session end is end.
- No full interactive mode (human editing verdicts at the gate) and no training/observation mode — post-v1.
- No full pre-build co-authoring conversation — v1 is gap-checking an existing spec.
- No cross-model/vendor decorrelation requirement, no other-agent adapters, no daemon, no user-configurable routing lists — seams only.
- Supervisors never write code, never run commands, never grade their own work.

## Accepted decisions
- D2: Review-error gate policy: on review failure (and for unknown/MCP tools and the recursion guard) the hook emits NO permissionDecision — stepping aside to the host's native permission flow — plus a log line; never a silent fail-open allow. — User chose 'no decision': Gadfly never vouches for an unreviewed action; 'defer' is headless-only, so omission is the step-aside mechanism.
- D3: Decision.ABSTAIN in the neutral contract: 'no verdict obtained — step aside, never vouch'; lowest merge precedence; adapters map it to host-native fall-through. — User chose replacing the fail-open allow with the host's own flow; a neutral enum value expresses that without leaking CC semantics into the core.
- D4: Human-edit feedback loop: the edit-ledger snapshots each AI edit's full content (not just a hash) so a later out-of-band human edit can be diffed against the builder's version; that diff feeds a SEPARATE idle-time memory-extraction agent (never the architect, never on the gate's hot path) that saves only worthy patterns as typed memory (project-only / cross-project style / personal). — User-authored, locked redesign: the extractor needs the real AI->human diff to judge whether a correction generalizes, and giving divergence its own consumer keeps it off both the architect's [anchor-missing] path and the gate's hot path. Replaces the abandoned SessionStart-reconciliation-to-architect design (reconcile.py removed).
- D7: Cover-for-other: a disabled supervisor is set to None and the survivor runs its *_solo prompt (solo=True), partially covering the other's lane; router config-flags + core SAFETY escalation route every action to the surviving supervisor so no lane goes unreviewed. — Lets a user run a single (cheap) model as sole supervisor without leaving commands, code, or architecture entirely ungated; separate solo prompt files keep each persona coherent instead of merging both — a deliberate relaxation of the strict two-supervisor separation-of-concerns.
- D8: Feedback learning runs as one idempotent, lock-guarded reconcile→extract routine (feedback_pass) driven by three triggers: SessionStart + PreToolUse nudge it detached/async behind a dedup-aware has_pending_work pre-check (mirroring the digest worker); Stop runs it inline as the 'learn before next turn' backstop. New models.feedback knob (default Sonnet) decouples the extractor from the code reviewer. — SessionStart-only detection left mid-session corrections invisible until the next session — and a builder re-edit could erase the divergence first. Async nudging from the review path captures a correction at the next reviewed action while keeping the LLM extraction off the gate's hot path (D4). The dedup-aware pre-check is mandatory because human edits cause permanent ledger divergence (D5), so a naive diverged() check would spawn a worker on every tool call. Refines, does not supersede, D4.
