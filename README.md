<div align="center">

<img src="assets/gadfly_hero.png" alt="Gadfly, a Socratic supervisor in your AI coding agent's loop" width="760">

**A Socratic supervision layer for AI coding agents.**

*“I am that gadfly which the god has given the state and all day long and in all places*
*am always fastening upon you, arousing and persuading and reproaching you.”*
— Socrates, in Plato's *Apology*

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen.svg)](pyproject.toml)
[![For Claude Code](https://img.shields.io/badge/for-Claude%20Code-8A2BE2.svg)](https://claude.com/claude-code)

</div>

---

Gadfly sits inside your coding agent's live tool-call loop and does what Socrates did to
Athens: it **questions every consequential move *before* it happens.** It grounds the work in
your vision, catches drift and real bugs pre-execution, surfaces the decisions that actually
matter, and learns your preferences as it goes, so you can stay at altitude instead of
babysitting a diff.

## Gadfly in action

| When your agent… | Gadfly… |
|---|---|
| makes a safe, routine change | ✅ **allows** it, silently |
| drifts from what you asked | 💬 **questions** it, mid-flight |
| writes a real bug | ⛔ **blocks** it before it runs |
| makes a big call your spec never settled | ✋ **surfaces** it to you |

*Four verdicts, checked on every tool call, all before anything executes. Gadfly stays quiet
when the work is on track, and speaks only when it matters.*

## The problem

An AI coding agent makes dozens of consequential decisions in a single session: a data
model, an auth strategy, a dependency, an edge case, a quiet deviation from what you asked
for. Almost none of them surface to you. You're left with two bad options:

- **Micromanage** every step, which defeats the entire point of an agent, or
- **Trust blindly**, and let drift, silent design choices, and latent bugs pile up.

Reviewing the code afterward doesn't close the gap. By the time a diff lands, an irreversible
command has already run, an early wrong turn already has later code stacked on top of it, and
the agent's consequential decisions are already baked into the project, most of them the kind a
diff never flags as a decision at all. The time to catch these is
while they're being made.

Gadfly is the third option: it sits in the loop and takes the review seat for you, so the agent
stays honest without you reading every line.

## How it works

At every tool call, **before it executes** (`PreToolUse`), Gadfly reviews the action with
two independent, read-only supervisors. A deterministic first pass auto-allows reads and safe
commands for free, so the LLMs only wake for things that matter. Each review returns one of
four verdicts:

| Verdict | What it means |
|---|---|
| ✅ **allow** | silent, stays out of your way (the common case) |
| 💬 **question** | a pointed note back to the builder that makes it reconsider, mid-flight |
| ✋ **surface** | pauses and asks *you*, for a genuinely consequential, undiscussed call |
| ⛔ **block** | stops an action that violates your spec or carries a real bug |

## Why you'll want it

- **Fewer keystrokes, higher altitude.** Gadfly replaces the human-in-the-loop role *as
  much as you want*: supervising, gating, and deciding on your behalf. You engage through
  surfaced questions, not constant approvals.
- **Nothing consequential slips by silently.** Every consequential decision the agent would
  otherwise make quietly is caught the moment it's made, then grounded in your spec, surfaced
  to you, or at minimum logged. Nothing unjustified goes unlogged.
- **Bugs caught *before* they run.** A skeptical code reviewer flags real logic errors,
  edge cases, broken contracts, and hallucinated APIs at the gate, not in a post-hoc review
  after they've already landed.
- **Stays true to your vision.** An architectural supervisor, loyal to *your* spec, catches
  drift and quiet betrayals of intent, like the agent satisfying the letter while missing the point.
- **It learns you.** Correct the agent's code and Gadfly notices: an idle-time loop distills
  your out-of-band edits into durable rules, so the same correction never has to happen
  twice. It calibrates to your style and gets sharper the more you use it.
- **Socratic, not bureaucratic.** Its main move is a question, not a block. It asks the thing
  that leads the agent to find the problem itself, and blocks only when it has to.

## The two supervisors

Two separate, isolated reviews, never merged, so one can't bias the other. Both run by default:

- **🏛️ The Architect** *(default: Opus)*. A Socratic visionary loyal to your vision.
  Reads code *as a language*, to grasp what's being built and why. Catches drift from the
  spec, inconsistency with the realized structure, and undiscussed decisions with lasting
  consequences. Questions first; asserts only when a question won't do.
- **🔬 The Code Reviewer** *(default: Sonnet)*. A logic skeptic, real defects only: wrong
  conditions, unhandled edges, races, leaks, broken invariants, misused or invented APIs.
  It stays silent on correct code.

Prefer to run lean? A **cover-for-other** mode lets a single model act as sole supervisor,
covering both lanes through a purpose-written prompt variant.

## It improves itself

Gadfly keeps a private, append-only **edit-ledger** of every change the agent makes. When
you edit that code out of band (the classic *“no, do it **this** way”*), Gadfly diffs your
version against the agent's and hands it to a separate, idle-time extractor that decides
whether the correction *generalizes*. Worthy patterns become durable memory:

- project-specific rules land in the supervised project's `claude.md`,
- cross-project style preferences land in your global memory.

All of it off the hot path, conservative by design (usually it saves nothing), and never
blocking the build.

## Grounded in your intent

Gadfly reasons from a small, layered memory of the supervised project rather than guessing:

| File | Owner | Role |
|---|---|---|
| `spec.md` | **You** *(required)* | The vision the architect enforces against, every gate |
| `claude.md` | **You** *(optional)* | Project rules, enforced when present |
| `codemap.md` | Builder | A live map of the current structure |
| `decisions.md` | Gadfly | A ledger of load-bearing decisions and why they were made |
| `memory.md` | Gadfly | Your cross-project style and calibration |

A light pre-build **midwife** pass reads your `spec.md` on the first prompt and asks the sharp
questions it leaves unanswered, so you sharpen a real spec before building instead of a vague one.

## Spec-driven development that actually holds

Most "spec-driven" workflows write a spec, then drift from it the moment coding starts, and it
becomes a stale doc nobody enforces. Gadfly makes the spec **load-bearing**:

- `gadfly init` **requires** a `spec.md`. Without one there's nothing to enforce.
- The architect measures **every** edit against it, in letter *and* spirit, and catches
  drift the moment it starts, not in review three hours later.
- Consequential calls your spec never covered get surfaced to you, and once you accept one it's
  promoted back into the spec, so the spec stays current instead of going stale.

## You set the altitude

An **autonomy dial** controls how often Gadfly involves you:

- **autonomous**: decides and logs almost everything; surfaces only the critical or irreversible
- **balanced**: surfaces the consequential, spec-silent, high-level calls; decides the rest
- **collaborative**: surfaces most consequential or conceptual decisions

Irreversible operations always ask, regardless of the dial.

## Architecture

A small, **pure core** wrapped by two adapter boundaries: one speaks the host agent's
native format, the other speaks to an LLM provider. Both are swappable; the core is
agent-agnostic, LLM-agnostic, and owns its own normalized state.

```mermaid
flowchart TB
    agent["🧑‍💻 &nbsp;Your coding agent<br/>Claude Code"]
    adapter["🔌 &nbsp;Agent Adapter"]
    core["⚙️ &nbsp;Pure Core<br/>Architect + Code supervisors · memory"]
    llm["🧠 &nbsp;LLM Provider<br/>Claude (subscription or API)"]
    agent -->|"every tool call"| adapter
    adapter -->|"review → verdict"| core
    core --> llm
    style agent fill:#161b26,stroke:#4a5568,color:#f0eae6
    style adapter fill:#1a2230,stroke:#3a5a7a,color:#f0eae6
    style core fill:#241a33,stroke:#8A48A4,color:#f0eae6
    style llm fill:#2a1e26,stroke:#EF816F,color:#f0eae6
```

See [`spec.md`](spec.md) for the full design.

## Quickstart *(v1: Claude Code)*

```bash
pip install git+https://github.com/Touchpoint-Labs/Gadfly.git   # zero deps; PyPI soon
cd your-project

# 1. Write a spec.md — it's required; the architect enforces against it. Sketch the project
#    with your AI assistant and save it as spec.md. (Optionally add a claude.md of rules.)

# 2. Wire Gadfly in — conflict-safe, leaves any hooks you already have.
gadfly init
gadfly status        # confirm it's live
```

Then use Claude Code as usual. Gadfly runs on your existing Claude Code subscription, so no API
key is needed. You can also point any supervisor at the Anthropic API instead, with the key read
from an environment variable, or mix the two: for example, the architect on the API and the code
reviewer on your subscription. Set this per role in `gadfly.toml`.

### Commands

| Command | |
|---|---|
| `gadfly init` | Wire hooks into this folder (a `spec.md` is required); `init global` targets `~/.claude` |
| `gadfly status` | Check the install is live — actually runs a hook end-to-end |
| `gadfly config` | Show, get, or set config in `gadfly.toml` (models, autonomy dial, review scope) |
| `gadfly disable` / `enable` | Pause / resume without touching your settings |
| `gadfly uninstall` | Remove Gadfly's hooks (leaves any of your own) |

## Configuration

`gadfly init` scaffolds a `gadfly.toml` in your project. Every key is optional and the defaults
are sensible, so you set only what you want to change. Read or change it with `gadfly config`, or
edit the file directly.

```toml
provider = "claude_cli"        # your Claude Code subscription (no key), or "anthropic_api"
autonomy = "balanced"          # autonomous | balanced | collaborative

[models]
architect = "claude-opus-4-8"  # the architect: drift, design, vision
code = "claude-sonnet-5"       # the code reviewer: bugs, edges, bad APIs
triage = "claude-haiku-4-5"    # fast command-safety triage

# Optional: run one supervisor on a different backend than the global provider.
# [providers]
# architect = "anthropic_api"
# [anthropic_api]
# api_key_env = "ANTHROPIC_API_KEY"   # the env var holding the key, never the key itself
```

A few more knobs, all optional:

- `disable_code_reviewer` runs the architect alone, and `disable_architect` runs the code reviewer
  alone. Either one covers both lanes (the cover-for-other mode). For a single supervisor we'd keep
  the architect: it holds on to Gadfly's Socratic voice and still catches real code bugs in solo
  mode. You can't disable both.
- `test_review` sets how edits to test files are reviewed: `both` supervisors, `code` only (the
  default), or `off`.
- `auto_allow_docs` is `true` by default, which skips review of documentation (`.md`) edits. Set
  it to `false` to have the architect review those too.
- `[memory]` sets a character budget per memory file (`spec`, `claude`, `codemap`, `memory`). When
  a file grows past its budget Gadfly compacts it, and `0` turns compaction off for that file.

Timing and retry knobs (`llm_timeout`, `llm_retries`, `poll_timeout`) exist as well, but rarely
need changing.

## Roadmap

- **More host agents.** Adapters, and a proxy layer for agents without hooks, to support other
  coding agents such as Codex, Gemini CLI, and Cursor.
- **More backends.** Local models, more API models, and more providers through the existing
  provider-neutral client.
- **A fuller pre-build pass** *(optional)*. Today the midwife only finds gaps in your spec. The
  next step is a real back-and-forth that produces a detailed spec and a project plan before any
  code is written.
- **Learning bootstrap** *(optional)*. Point Gadfly at your past sessions so it starts with your
  patterns already extracted, instead of from an empty memory.

## License

[MIT](LICENSE). © 2026 Touchpoint Labs.
