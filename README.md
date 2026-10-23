# taskgen_poc: Rainbow-Teaming-style task generation for FAIR Forge

Goal: hand an SME a TB4 (Harbor) task that targets a weakness real agent trials have shown, has a real kforge grader,
and has been built and graded once by an evaluator, so the SME's work is running it on Astra, hardening it if Astra
passes, and writing the golden solution if Astra fails.

Every stage follows the documents in `guidelines/` (vendor guidelines, the function-first primer, client feedback,
generation rules, the general agent guideline). Generation is always split by subdomain.

```
mine (once)          past trials ──> limitations.json (the model's weaknesses)
grid                 limitation x subdomain cells, each with an elite (archive.json)
per draft:
  sample cell ──> prompt adapter (concept) ──> generator (draft) ──> rule check + novelty
  evaluator loop:  static review ──> kforge kit ──> evaluator build + grade ──> assessment
                   └─ revise (cheap model) and repeat, or ready ──> elite of its cell
package              ready drafts ──> TB4 / CF-1.0 task folder + zip
```

| Part | File | What it does |
|---|---|---|
| Mining | `mine.py` | Reads the hand-picked evaluated tasks in `domains/<d>/trials.json`; separates model limitations from task defects and infra; writes `limitations.json`. |
| Grid / archive | `archive.py` | Limitation x subdomain grid with one elite per cell (replaced only by higher fitness), further descriptors (phenomenon, design freedom, reasoning) sampled toward under-represented values, failed attempts kept with reasons, novelty check (cheap model). Existing tasks are indexed only for the novelty check. |
| Prompt adapter | `adapter.py` | Rainbow Teaming's mutator (cheap model): turns a cell into a task concept, or mutates the cell's elite concept toward higher fitness. Sees only the limitation's mechanism, the cell, the guidelines and evaluator feedback. |
| Generator | `sample.py` | Claude Opus (high effort) writes the full draft from the concept. It never sees existing tasks, so the mined tasks cannot collapse diversity. Plans every grader check with a kforge check id. Rule check (`lint.py`) and novelty check filter it. |
| Static review | `judge.py` | Cheap model (default `moonshotai/kimi-k3` over OpenRouter): SKILL.md + guidelines + an Astra estimate. "not a forge task" stops the draft at once. |
| Kit | `kit.py`, `catalog.py` | Builds the kforge grader (`requirements.yaml`, catalogue, models) from the draft and the kforge validator catalogue, builds the starter KiCad project from the ports, validates both in the image (empty and starter-only submissions must fail cleanly). |
| Evaluator build | `solver.py`, `sandbox.py` | The cheap model designs the circuit in the `forge-ee-grader` image (no network) with the reference outline and a grade tool, until a passing design (a good run). `--probe` adds a blind attempt. |
| Assessment | `evaluate.py` | Combines review, kit coverage and runs: outcome (feasible / grader problem / infeasible / inconclusive), Astra estimate, fitness = 1 - p for feasible runs. Ready = feasible + good run + p <= `astra_target`. |
| Loop | `refine.py` | Review -> build -> assess, revising with Claude Sonnet 5.5 at medium effort between rounds (`--rounds`). |
| Package | `package.py` | CF-1.0 / TB4 layout (`tasks/lp1-broadband-match` as reference): kforge grader in `tests/` (kforge vendored), starter and inputs in `environment/`, handbook outside `environment/`, failing `solution/solve.sh`, the evaluator's passing design in `sme/`, `SME_NOTES.md`. |

## Run

```
cd taskgen_poc
export ANTHROPIC_API_KEY=...        # generator, reviser, kit builder
export OPENROUTER_API_KEY=...       # adapter, reviewer, novelty, evaluator, assessment (OR_API_KEY also works)
uv run mine.py --domain electrical  # once, and again when trials.json gains tasks
uv run run.py --list                # the subdomains
uv run run.py --subdomain "power electronics" --n 10
uv run run.py --run runs/<id> --from review    # re-run the evaluator loop
```

Needs Docker with `forge-ee-grader:2026-10-09` (it runs as amd64; on Apple silicon it is emulated and slower).
`--static-only` skips the build. Output: `runs/<id>/drafts/<slug>/` (draft, rounds/, kit/, eval/, evaluation.json),
`runs/<id>/packages/<slug>.zip` for ready drafts (`--package-failed` packages all), and `runs/<id>/cost.json`.

## Cost controls

- Revisions use Claude Sonnet 5.5 at medium effort (`TASKGEN_REVISE_MODEL`, `TASKGEN_REVISE_EFFORT`).
- Every Claude system prompt has a 1-hour cache breakpoint (guidelines and the check catalogue repeat every call).
- "not a forge task" stops a draft before any revision; the build runs only after the rules pass.
- The adapter, reviewer, evaluator and assessment run on the cheap model (`TASKGEN_REVIEW_MODEL`); OpenRouter
  requests set `data_collection: "deny"`.
- `runs/<id>/cost.json` records tokens and dollars per model and purpose for every invocation.

## Domains

`domains/<name>/` holds `domain.json` (discipline, deliverable contract, images, check families, tool names, budgets),
`axes.json` (subdomains with their families, descriptors, secondary axes), `seeds.json`, `trials.json`. Aero and
mechanical are new folders plus their kforge check families and images.

## What a ready package is and is not

- Is: a short brief aimed at one observed limitation, a separate handbook, a real kforge grader that an evaluator's
  design passed, an Astra estimate with the binding checks, and the evaluator's design as a starting point.
- Is not: a released task. There is no golden solution (`solution/solve.sh` fails on purpose), the Astra figure is an
  estimate, and the limits still need an SME's review. Run it on Astra (8 attempts) before anything else.

### Executable readiness and evaluator configuration

The design evaluator defaults to `xiaomi/mimo-v2.6-pro` on OpenRouter. Override it with
`TASKGEN_EVALUATOR_MODEL`; `TASKGEN_REVIEW_MODEL` still controls review/assessment. The evaluator
has a maximum 600-second wall budget. Timeout records reward 0 and an inconclusive outcome,
not evidence of difficulty or infeasibility.

Static-only review never marks a draft ready; unavailable Docker is an explicit error. Ready means
an executable passing design, a validated kit, separately authored executable coverage tests, and
no unresolved feasibility/model/measurement blockers. Difficulty estimates are advisory, and archive
fitness records feasibility rather than optimizing estimated target-model failure. Actual target-model
trial calibration is still required before claiming benchmark difficulty.

The kit audit checks each requirement and authors at least six tests per requirement (two valid designs,
a violation, a boundary, an invariance, and a guard fault). Tests run in the image; missing, skipped or
failing cases block the kit. This is additional model-assisted evidence, not a substitute for an engineer's
independent physics review. Missing authoritative models must be supplied or the draft revised; the pipeline
must not fabricate them to pass. Existing incomplete drafts are not automatically repaired or certified.

Evaluation preserves all output files. Packaging loads kit-local extensions through the same runner and
requires evidence tied to the current draft hash. Regression checks: `.venv/bin/python -m unittest discover -s tests -v`.

### Run selected steps

`run.py` defaults to **sampling only**. Use `--full` explicitly for the entire workflow.

```sh
uv run run.py --step sample --n 5
uv run run.py --run runs/<id> --step stage1
uv run run.py --run runs/<id> --step static
uv run run.py --run runs/<id> --step evaluate
uv run run.py --run runs/<id> --step package
uv run run.py --step sample stage1 --n 5
uv run run.py --full --n 5
```

- `sample`: generate drafts.
- `stage1`: apply only SKILL.md Stage 1 idea screening (advance/reshape/drop, rank, reasons and next inputs).
  Saves `stage1_review.json` and `.md` per draft and a ranked summary at the run root. It does not revise drafts,
  execute Docker, or alter readiness. Screening results are advisory; full execution does not automatically
  discard ideas on the basis of this report.
- `static`: draft review against the full-task rules, with packaging checks deferred; no Docker or revisions.
  Writes `static_review.json`. It does not modify the draft, Stage 1 report, or executable evaluation evidence.
  This is not a completed-package certification and cannot grant readiness.
- `evaluate`: the static review, revision, kit audit and executable evaluation loop.
- `package`: package ready drafts (`--package-failed` includes unfinished drafts).
- `--full`: sample → stage1 → evaluate (includes static review) → package.

`--step` accepts multiple steps in the order given. Steps that do not include sampling require `--run`.
`--from` runs a step and subsequent steps; the legacy `--from review` aliases `--from evaluate`.

Each `evaluate` invocation replaces that draft's previous `rounds/`, `kit/`, `eval/`, `evaluation.json`, and
`skill_check.json`. It preserves `stage1_review.*` and `static_review.json`, so idea screening, static review, and
executable evaluation remain separate. The reviewer and design evaluator are configured independently with
`TASKGEN_REVIEW_MODEL` and `TASKGEN_EVALUATOR_MODEL`.

Generation-support roles are configured separately: `TASKGEN_ADAPTER_MODEL`, `TASKGEN_NOVELTY_MODEL`, and
`TASKGEN_INDEX_MODEL` default to `moonshotai/kimi-k3`. MiMo can therefore be reserved for review and executable
evaluation without using it for concepts, indexing, novelty checks, or draft writing.

Choose the draft generator without running the evaluation loop:

```sh
uv run run.py --step sample --n 5 --generator-model xiaomi/mimo-v2.6-pro
uv run run.py --step sample --n 5 --generator-model claude-sonnet-5-5
```

Bare `claude-*` IDs use Anthropic (`ANTHROPIC_API_KEY`); `provider/model` IDs use OpenRouter
(`OPENROUTER_API_KEY` or `OR_API_KEY`). The default is `TASKGEN_GENERATOR_MODEL`, falling back to
`claude-opus-5-5`. This selects the draft writer; concept adaptation and novelty checks use their dedicated models.
Sampling retains its lint/novelty filters but does not invoke the evaluation/revision loop.
Each draft records its `generator_model`.

Before each writer call, the exact secret-free request is saved at
`runs/<id>/generation_prompts/<draft-number>/attempt-<n>.json`. It includes the assembled system instructions,
dynamic concept/cell prompt, full response schema, model, reasoning level, token limit, and provider route. The file
is written before the call, so failed or interrupted generations remain inspectable.

### Time and cost measurement

Sampling writes `runs/<id>/sampling_timing.json` live. For each candidate it records concept time, every generation
attempt, lint time, novelty-review time, retries, and total wall time until the draft passes lint and novelty. Archive
indexing is reported separately. `mean_sampling_acceptance_seconds` is the steady-state mean for accepted drafts;
`end_to_end_seconds_per_accepted` also includes indexing and skipped candidates.

Every `run.py` invocation appends to `runs/<id>/pipeline_metrics.json`. A `--full` record contains total elapsed time,
time and model cost by step, generated/ready/packaged counts, total cost, cost per generated draft, and cost/time per
ready task. A task counts as ready only after the executable readiness gates pass. Failed and interrupted invocations
are also recorded. `cost.json` remains the cumulative model-cost ledger.
