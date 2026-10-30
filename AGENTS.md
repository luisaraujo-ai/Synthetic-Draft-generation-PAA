# AGENTS.md

Orientation for coding agents. **[Observed]** = verified in the repo. **[Proposed]** = a suggestion, not an existing
convention. Detail lives in [README.md](README.md); this file does not repeat it.

## What this is

`taskgen_poc` (package name `taskgen-poc`): a Python pipeline that generates *engineering design task drafts* (a brief
plus a separate handbook) for the FAIR Forge benchmark. It mines model limitations from past trials, samples a cell,
drafts a task with an LLM, lints it, and has an LLM judge review it. There is **no execution backend**: nothing runs
KiCad, Docker or a grader here ([README.md](README.md) top, "Run"). [SKILL.md](SKILL.md) is the Forge check used as
the Stage 1 screen (`run.py --skillcheck`) and is itself fed to a model.

## Layout [Observed]

- Flat top-level modules, no package. Pipeline order: `mine.py` -> `sample.py` (Sampler + `Draft` schema + generator)
  with `adapter.py` (concept step) -> `lint.py` -> `judge.py` (split spec/difficulty review) -> `run.py` (serial) or
  `parallel_run.py` (SQLite queue, coordinator + worker processes). Table of roles: README "taskgen_poc" section.
- `llm.py`: all model access (Anthropic SDK for `claude-*`, OpenRouter over urllib otherwise), cost ledger, prices, and
  every `TASKGEN_*` env override. `domain.py`: `Domain` loader, `guidelines()` injector, `CANARY`.
- Post-processing: `idea_review.py` (Stage 1), `batch_novelty.py`/`novelty.py`, `rejudge.py`, `export_gallery.py`.
- `task-gallery/`: separate Vercel app (`api/index.py`, own `requirements.txt` with psycopg). Setup in its
  [README](task-gallery/README.md). `gallery_data/*.zip` is committed on purpose.
- `experiments/`, `harbor-tasks/`, `ws_tmp/`: scratch/private work, gitignored.

## Setup and commands [Observed]

- Python >=3.10, deps in [pyproject.toml](pyproject.toml) (`anthropic`, `pydantic`; ). `uv.lock` is
  gitignored. The repo has a `.venv`; README uses both `uv run` and `.venv/bin/python`.
- Keys come from the shell, never files: `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY` (or `OR_API_KEY`).
- Generate: `uv run run.py --list`, `uv run run.py --subdomain "power electronics" --n 10`,
  `parallel_run.py --n 50 --generation-workers 3 --judge-workers 2` (README has resume, budget, screen usage).
- Tests (offline, model calls stubbed): `.venv/bin/python -m unittest discover -s tests -v`. Tests add the repo root to
  `sys.path` and replace `adapter.adapt`, `llm.generate_parse`, `judge.review` with stubs.
- **No linter, formatter or type checker is configured** (no ruff/mypy/pyright/pytest in pyproject or README). Do not
  claim to have run one. `py_compile`/unittest are the only checks available.

## Known state at time of writing (re-verify, this will drift)

- `taskio.py` holds the shared `read`/`atomic` helpers and `plan()` (publishability gates) used by `export_gallery.py` and
  `batch_novelty.py`. Google Drive/Sheets publishing (`publish_drive.py`) and `legacy/` were removed.
- `task-gallery/` has no tests directory; `tests/test_gallery.py` covers it from the root.

## Conventions observed

- Style: 4-space indent, single quotes, module docstring explaining the module's contract, comments only for the
  non-obvious "why". Some files (`parallel_run.py`, `batch_novelty.py`) are denser and
  unspaced (`a=b`); match the file you edit rather than reformatting it.
- Structured LLM output uses pydantic models (`Draft`, `Review`, `Concept`); calls go through `llm.py`, which logs
  tokens and cost per call. New model calls must be logged there so `cost.json`/budgets stay correct.
- Failure messages in `lint.py` start with `rule N:` / `output size:`; `lint.split()` routes them by prefix
  (`STYLE`) into "edit call" vs "regenerate". New lint checks need the right prefix to route correctly.
- Writers: `domains/<d>/ledger.json`, `tasks/`, `summary.json`, `cost.json` are written by one coordinator only,
  guarded by `pipeline_lock.py` (`fcntl`, so macOS/Linux only). Never add a second writer or bypass the lock.
- Durable state is written atomically (`parallel_run.atomic`) and runs are resumable; keep new stages idempotent and
  checkpointed the way existing ones are.

## Constraints that are easy to break

- **Guidelines are prompt input.** `domain.guidelines()` concatenates every `guidelines/*.md` (sorted by name) into the
  generator, concept step and judge, unchanged (README Notes; ~19k tokens, 1-hour cache breakpoint in `llm.py`).
  Adding or editing a file changes every prompt, cost, cache hits, and judge verdicts. `guidelines/` is **gitignored**
  yet required at runtime: a fresh clone has none. Read [guidelines/](guidelines/) before touching `lint.py`/`sample.py`;
  `lint.py` implements rules 1-5 and 9 of `guidelines/04_generation_rules.md`.
- `domains/` is also gitignored and required (`domain.json`, `axes.json`, `limitations.json`, `ledger.json`).
  `ledger.json` drives coverage balancing and "already drafted" lists; never hand-edit or delete during a run.
- `runs/` is gitignored output; many reproducibility artifacts live there. Do not delete or rewrite old runs;
  novelty checks and the gallery export read them (`runs/*/tasks`, `drafts/*/draft.json`).
- Briefs must stay standalone and never name tools, benchmark/grader words or the canary (lint rules 1, 4). The
  packager, not the generator, adds `CANARY` (`domain.py`).
- Judge verdicts are one model's opinion; mandatory defects block, preferences and difficulty are advisory (README).
  API/transport failures must not become engineering rejections (README, parallel section).
- Spend: budgets are local estimates, not billing guarantees. Do not run live generation/judge batches without the user
  asking; each call costs money and the README states the parallel changes are not live-tested.
- Secrets: `.env*` is gitignored; never commit keys or gallery passwords.
- `llm.py` model defaults and its module docstring disagree (docstring mentions Opus/kimi/`refine.py`; code defaults to
  Sonnet 5.5 and MiMo). Trust the constants, and do not "fix" one without checking the other.

## Pitfalls seen in the code

- `domain.py` `REPO` points to a sibling `../forge-ee-verifiers-library` that may not exist; only `mine.py` uses it.
- `SSL_CERT_FILE=/tmp/taskgen-ca.pem` is used in README commands for provider access; a missing file breaks requests.
- `lint.validate_spice` silently skips when `ngspice` is not installed, so SPICE parse failures may go unnoticed locally.
- Word limits in `lint.py` come from `domain.json` and are guidance-adjacent (guideline 04 says word count alone must
  not trigger rejection); check both before changing a limit.

## Proposed conventions (not yet in the repo; need your approval)

- [Proposed] Keep a tracked copy or seed of `guidelines/` and `domains/` so a fresh clone can run the tests and pipeline.
- [Proposed] Add a minimal check command (e.g. `python -m compileall .`) and document it once chosen.
- [Proposed] Run only the stubbed unittest suite for changes; require explicit user approval for any live model call.
