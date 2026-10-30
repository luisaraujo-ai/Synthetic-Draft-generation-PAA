# Handoff

Read this first, then [README.md](README.md) (usage detail) and [AGENTS.md](AGENTS.md) (conventions and traps).

## What this is

A Python pipeline that generates engineering-design task drafts (a brief plus a separate handbook) for the FAIR Forge
benchmark: mine model limitations from past trials, sample a cell, draft with an LLM, lint, LLM-judge, Stage 1 screen,
batch novelty, gallery export. There is no execution backend: nothing here runs KiCad, Docker or a grader.

## Before you can run anything: ask Luis

Two gitignored directories are required at runtime and are **not in the repo**:

| Needed | Why | Where |
|---|---|---|
| `guidelines/` (5 files: `01_...` to `05_...`) | Concatenated verbatim into the generator, concept step and judge prompts. Lint rules 1-5 and 9 implement `04_generation_rules.md`. | Shared Drive folder. **Ask Luis for access.** |
| `domains/electrical/` (`domain.json`, `axes.json`, `limitations.json`, `ledger.json`, `trials.json`) | Domain config, mined limitations, and the ledger that drives coverage balancing. | Not on the Drive folder as far as I know. Ask Luis. |

Put them at the repo root unchanged. Editing any guideline changes every prompt, the cost, cache hits and judge verdicts,
so do not edit them casually. Past runs (`runs/`, also gitignored) can be requested too if you need novelty history:
novelty checks and the gallery export read them.

## Setup

- Python >= 3.10. `uv sync` (or use the existing `.venv`).
- Keys come from your shell only: `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` (or `OR_API_KEY`). Never commit them.
- Some README commands use `SSL_CERT_FILE=/tmp/taskgen-ca.pem`; drop it if you do not need a custom CA.
- Offline tests (all model calls stubbed): `python -m unittest discover -s tests -p 'test_[!o]*.py'`.
  The pattern skips `tests/test_outputs.py`, a gitignored FreeCAD scratch file that needs pytest.
  No linter or type checker is configured.

## Pipeline map

Code is the `taskgen/` package; the root scripts are thin entry points over `taskgen/cli/`.
`mine.py` -> sampler + `concepts` + `generator` + `lint` (`taskgen/generation/`) -> `review/judge.py` -> `run.py`
(serial) or `parallel_run.py` (SQLite queue, `taskgen/pipeline/parallel/`) -> `run.py --skillcheck` (Stage 1,
`review/stage1.py`, uses `SKILL.md`) -> `batch_novelty.py` -> `export_gallery.py` -> `task-gallery/` (Vercel app).
`taskgen/llm/` is the only place that talks to models and the only place cost is tracked.
`pipeline/storage.py` and `publishing/plan.py` hold the shared JSON helpers and the publishability gates.

## State at handoff

- Three runs in `runs/` (Oct 23-24). Latest, `20261024-102325-parallel-0ff97d`: 28 accepted, 2 unjudged, about $19 total
  (about $0.70 per accepted task). Stage 1: 23 `advance`, 2 `reshape`. Batch novelty selected 23.
- Google Drive/Sheets publishing and the old `legacy/` kforge pipeline were removed. The gallery is the publishing path.
- Gitignored scratch left on disk locally (not part of the handoff): `mechanical_tasks/`, `forge-sme-kit/`,
  `experiments/`, `ws_tmp/`, `.repo-backups/`.

## What the pipeline can and cannot claim

- Strong: guideline-compliant, standalone briefs; Stage 1 pass rate; internal diversity and duplicate avoidance.
- Not validated: **difficulty and feasibility.** "Hard" is an LLM judge's estimate plus mined past failure modes. No
  reference solution or solver has been run against these tasks. Nearly every Stage 1 review flags "no passing design
  demonstrated", unmeasured grader runtime and provisional thresholds. Stage 1 gives almost every task rank 8, so it
  does not discriminate well.
- Novelty is checked only against accepted tasks and the current batch, not the wider benchmark.
- Judge verdicts are one model's opinion (MiMo judges what Sonnet generates).

Suggested next steps: reference-solve a handful of tasks in ngspice to measure how often the author claims hold; run a
few against a real solver model to check difficulty; widen the batch to see if Stage 1 ranks spread.

## Traps

- **Do not run live generation or judge batches without sign-off.** Every call costs money. Budgets are local estimates,
  not billing guarantees. The parallel runner is not live-tested.
- One coordinator writes `ledger.json`, `tasks/`, `summary.json`, `cost.json`, guarded by `taskgen/pipeline/locks.py`
  (macOS/Linux only). Never add a second writer. Never hand-edit or delete the ledger during a run.
- Do not delete or rewrite old `runs/`.
- New model calls must go through `taskgen/llm/` so cost stays correct. New lint messages need the `rule N:` / `output size:`
  prefix so `lint.split()` routes them.
- Model defaults live in `taskgen/llm/settings.py` (Sonnet 5.5 generator/concept/novelty, MiMo judge). Read them as `llm.settings.NAME`.
- `generation/lint.validate_spice` silently skips if `ngspice` is not installed.
- `taskgen/mining/miner.py` `REPO` points at a sibling `../forge-ee-verifiers-library` that may not exist; only the miner uses it.
- Gallery secrets (`.env*`, passwords) are never committed. See [task-gallery/README.md](task-gallery/README.md).
