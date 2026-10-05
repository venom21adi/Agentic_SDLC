# Skill-file shortlist per agent

Research date: 2026-10-04. Repo facts (stars, license, last push) come straight from the GitHub API, not from search
snippets. File sizes are my token estimates (bytes / 4). Every file listed was downloaded and skimmed; **none has been
integrated yet**. Before integration each chosen file gets read in full.

## What was searched and what was excluded

| Source | Stars | License | Last push | Verdict |
|---|---|---|---|---|
| [obra/superpowers](https://github.com/obra/superpowers) | 295,201 | MIT | 2026-09-27 | Use. Concrete, opinionated, written as subagent prompts. |
| [github/spec-kit](https://github.com/github/spec-kit) | 140,104 | MIT | 2026-10-03 | Use. Best spec/plan/tasks templates; commands assume its own CLI. |
| [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) | 101,112 | MIT | 2026-10-03 | Use. Broadest coverage; compact personas for reviewer and tester. |
| [wshobson/agents](https://github.com/wshobson/agents) | 40,192 | MIT | 2026-10-04 | Marginal. Mostly capability keyword lists; some files assume an orchestrator's file layout. |
| [VoltAgent/awesome-claude-code-subagents](https://github.com/VoltAgent/awesome-claude-code-subagents) | 25,496 | MIT | 2026-09-21 | Weak. Same template for every role (checklists of keywords); business-analyst is BPMN/ROI process work, not ticket decomposition; assumes a "context manager" protocol we don't have. |
| [bmad-code-org/BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD) | 53,778 | **unasserted** (GitHub reports `NOASSERTION`) | 2026-10-04 | **Excluded** until its license is clarified. Not safe to copy into this repo. |

## Fit problems every candidate shares (so adaptation is needed)

These files are written for **interactive coding assistants with tools** (read files, run tests, ask the user, spawn
subagents). Our agents are **single non-interactive model calls that must return one JSON object**. So each adopted
file is an *adapted derivative*: interactive steps and tool names removed, and a line added saying the output contract
in our prompt wins. MIT requires keeping the copyright and permission notice, so each derivative keeps its source URL,
commit SHA, license text and a note of what was changed.

Critic skills have an extra constraint: a critic must see only the artifact. superpowers' `task-reviewer-prompt` tells
the reviewer to read the implementer's *report*; that input is dropped and only its "do not trust claims, verify
against the code" stance is kept.

## Shortlist

### 1. Business analysis (ask -> features, tickets, dependency graph)
Our first real-model run split a small CSV tool into 15-20+ tickets (separate tickets for `--help` text and the README).
Each ticket costs about eight agent stages, so sizing guidance is the highest-value thing here.

| Rank | File | Tokens | Why |
|---|---|---|---|
| 1 | addyosmani `skills/planning-and-task-breakdown/SKILL.md` | ~2.6k | Dependency graph first, vertical slices, an XS-XL sizing table ("L or larger: break it down", "'and' in the title means two tasks", "S and M work best"). Best match for DAG output. |
| 2 | superpowers `skills/writing-plans/SKILL.md` (the *Task Right-Sizing* section) | ~2.6k whole file | Directly targets over-splitting: "fold setup, configuration, scaffolding and documentation into the task whose deliverable needs them; split only where a reviewer could meaningfully reject one task while approving its neighbour." Use the section, not the whole file. |
| 3 | spec-kit `templates/tasks-template.md` | ~2.3k | Story-grouped phases and `[P]` parallel markers. Markdown checklist format, so it needs translating to our JSON. |

### 2. Planning
| Rank | File | Tokens | Why |
|---|---|---|---|
| 1 | superpowers `skills/writing-plans/SKILL.md` | ~2.6k | File structure, global constraints, task structure. Some of it assumes git and plan files; trim. |
| 2 | spec-kit `templates/plan-template.md` | ~0.9k | Small and structured; includes a constitution check against project principles. |
| - | wshobson `plugins/ship-mate/agents/architect.md` | ~1.2k | Not shortlisted: reads and writes orchestrator files we don't have. |

### 3. Spec author
| Rank | File | Tokens | Why |
|---|---|---|---|
| 1 | spec-kit `templates/spec-template.md` | ~1.1k | User stories with priorities, functional requirements, edge cases, measurable success criteria, assumptions. Small and fits our spec JSON. |
| 2 | addyosmani `skills/api-and-interface-design/SKILL.md` | ~3.7k | Contract-first interface design. Our specs need exact interface contracts. |
| 3 | addyosmani `skills/spec-driven-development/SKILL.md` | ~3.2k | Six areas a good spec covers (objective, commands, structure, style, testing, boundaries). Overlaps with 1. |
| - | spec-kit `templates/commands/specify.md` | ~4.6k | Not shortlisted: interactive (asks clarifying questions) and tied to spec-kit scripts. |

### 4. Plan critique
| Rank | File | Tokens | Why |
|---|---|---|---|
| 1 | superpowers `skills/brainstorming/spec-document-reviewer-prompt.md` | ~0.4k | Checks completeness, consistency, clarity, scope, YAGNI, and has a calibration rule: "only flag issues that would cause real problems; approve unless serious gaps". That addresses over-strict critics burning retries. |
| 2 | spec-kit `templates/commands/analyze.md` | ~2.9k | Cross-artifact consistency, a four-level severity rubric (CRITICAL..LOW), compact report. Needs de-interactivating. |
| 3 | addyosmani `skills/doubt-driven-development/SKILL.md` | ~4.1k | Adversarial self-doubt. Larger, and partly interactive. |
| - | wshobson `architect-review.md`, VoltAgent `architect-reviewer.md` | ~1.7-1.9k | Not shortlisted: generic keyword lists, little actionable behaviour. |

### 5. Implementation
| Rank | File | Tokens | Why |
|---|---|---|---|
| 1 | superpowers `skills/subagent-driven-development/implementer-prompt.md` | ~1.6k | Written for a fresh subagent given one task, the closest structural match to ours. References tools; trim. |
| 2 | superpowers `skills/test-driven-development/SKILL.md` | ~2.4k | Tests first. Our implementer is single-shot, so adapt to "write tests and code together, tests that would fail without the code". |
| 3 | wshobson `plugins/python-development/agents/python-pro.md` | ~1.7k | Python-specific idioms; a keyword list rather than behaviour, so lower value. |
| - | addyosmani `incremental-implementation` | ~2.3k | Thin-slice cycles assume iterating, which a single call can't. |

### 6. Code critique
| Rank | File | Tokens | Why |
|---|---|---|---|
| 1 | addyosmani `agents/code-reviewer.md` | ~1.0k | Compact persona: five axes (correctness, readability, architecture, security, performance) plus an output template. |
| 2 | addyosmani `skills/code-review-and-quality/SKILL.md` | ~5.2k | The detailed rubric behind it ("review the tests first", change sizing). Large; excerpt it. |
| 3 | superpowers `skills/subagent-driven-development/task-reviewer-prompt.md` | ~2.2k | "Treat the report as unverified claims; verify against the code." Keep the stance, drop the report input. |
| opt. | addyosmani `skills/security-and-hardening/SKILL.md` | ~4.4k | Security supplement. (My scan flagged one line; it is the rule "never use `eval()`", benign.) |

### 7. QA strategy
| Rank | File | Tokens | Why |
|---|---|---|---|
| 1 | addyosmani `agents/test-engineer.md` | ~0.8k | Test at the right level, the Prove-It pattern, scenarios to cover. Compact. |
| 2 | superpowers `skills/test-driven-development/writing-good-tests.md` | ~2.1k | "Every test names the break it catches"; derive expectations independently, no mirror assertions. Good basis for what a strategy should demand. |
| - | VoltAgent `qa-expert.md`, wshobson `ship-mate/agents/qa.md` | ~1.4-1.7k | Not shortlisted: generic template, or assumes an orchestrator and executes tests itself. |

### 8. Test executor
No skill. It makes no model calls.

## Proposed comparison sets (for the real-ask test)

| Set | BA | Planning | Spec | Plan critic | Implementer | Code critic | QA |
|---|---|---|---|---|---|---|---|
| **baseline** | none | none | none | none | none | none | none |
| **A: addyosmani** | planning-and-task-breakdown | planning-and-task-breakdown | spec-driven + api-design | doubt-driven | incremental (adapted) | code-reviewer + code-review-and-quality | test-engineer |
| **B: superpowers** | right-sizing | writing-plans | none | spec-document-reviewer | implementer-prompt + TDD | task-reviewer (stance only) | writing-good-tests |
| **C: spec-kit** | tasks-template | plan-template | spec-template | analyze | none | none | none |

Sets are mixed by role on purpose: no single repo covers every role well. With one or two runs on a stochastic model
this is a first signal, not a ranking.

## Cost note

Real-model usage so far: two calls, about $0.01. A full run is roughly 8 stages per ticket plus retries, so a
15-20 ticket ask is on the order of 1-3M tokens (about $0.5-2 at DeepSeek peak list prices). The default token cap
is 1M, which a multi-feature ask will hit; I'd raise it to 3M for the real run.
