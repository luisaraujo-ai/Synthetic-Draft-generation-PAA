# taskgen_poc: standalone task + handbook generation for FAIR Forge

Goal: output engineering design tasks (a brief and a separate handbook) that follow the client guidelines, target a
model failure mode already seen in real trials, look hard to the LLM judge, and are standalone: they assume nothing
exists beyond what the brief states or the task writes out itself. Accepted tasks go to the SKILL.md check by hand.
There is no execution backend (no KiCad, kforge, Docker, evaluator build or packaging). That comes later, once
standalone generation is consistent.

```
mine (once)   past trials ──> limitations.json
per candidate:
  sample cell ──> concept ──> draft ──> lint ──> judge ──> accepted task
  (limitation x subdomain      (generator)  (rules   (guidelines + standalone +
   x descriptors)                            1-5, 9)  difficulty vs. astra_target)
          ├── wording problems (tools, settings, preferences, length): edit call on brief + handbook only
          ├── judge failures that are text-only (brief criteria): edit call, then re-judge once
          └── structural lint problems, other judge failures, too easy: regenerate (--retries)
```

| File | What it does |
|---|---|
| `mine.py` | Reads hand-picked evaluated trials (`domains/<d>/trials.json`) and writes `limitations.json`. |
| `sample.py` | Cell sampling (confidence-weighted, empty cells first, descriptors toward under-represented values) and the generator (`Draft` schema, prompts). |
| `adapter.py` | Concept step (Claude Sonnet 5.5 by default, `TASKGEN_ADAPTER_MODEL`): cell -> task concept. |
| `lint.py` | Mechanical rules (split into wording problems an edit call fixes and structural ones): word limits, no tools or benchmark words in the brief, no preference wording, every referenced file exists, SPICE files parse. |
| `judge.py` | Split specification/difficulty review (MiMo by default), 60k tokens per stage. Mandatory defects block acceptance; secondary preferences and difficulty estimates are advisory. Completed stages can be checkpointed. |
| `parallel_run.py` | Persistent parallel generation and judge queues, novelty checks, resume and centralized publishing. |
| `run.py` | The serial loop and the outputs. `--skillcheck <run>` runs the SKILL.md stage 1 screen (`idea_review.py`) on a run's tasks. |
| `legacy/` | The previous pipeline (kforge kit, evaluator build, packaging, archive/novelty, tests) for when verifier coupling returns. |

## Run

```
export ANTHROPIC_API_KEY=...        # generator (claude-* models)
export OPENROUTER_API_KEY=...       # concept, judge (OR_API_KEY also works)
uv run run.py --list
uv run run.py --subdomain "power electronics" --n 10 [--retries 2] [--generator-model xiaomi/mimo-v2.6-pro]
uv run run.py --skillcheck runs/<id>
.venv/bin/python -m unittest discover -s tests -v    # offline, model calls stubbed
```

Output in `runs/<id>/`: `tasks/<slug>/` (instruction.md, handbook.md, inputs/, draft.json, judge.json) for accepted
tasks; `too_easy/` for drafts that pass the guidelines but never looked hard; `rejected/` for every attempt sent back,
with why; `prompts/` (exact generator requests), `cost.json`, `summary.json`.
`domains/<d>/ledger.json` records every candidate: it drives coverage balancing and the "already drafted" list.

## Notes

- The judge's pass probability is one model's opinion, not a measurement. Treat it as a filter, calibrate against trials.
- The generator is told the contract in `domains/<d>/domain.json` (`deliverable_contract`): standard file formats,
  inputs stated or written out in full, no starter project, library, vendor model or datasheet.
- Guidelines in `guidelines/` are injected into the generator, concept step and judge unchanged.
- Generation is repair-free by design: no "diagnosis and repair" task form (it forced a supplied failing netlist). The
  `Draft` schema carries only what the judge and a reviewer read, to keep each generation short.

## Parallel generation with a persistent judge queue

Use `parallel_run.py` for a new batch. It shares the concepts, generation, lint, split MiMo judge and packaging
code with `run.py`; it does not launch multiple independent ledger writers.

```bash
SSL_CERT_FILE=/tmp/taskgen-ca.pem .venv/bin/python -u parallel_run.py \
  --n 50 --generation-workers 3 --judge-workers 2
```

`--n 50` means **50 candidate slots**, each with up to `--retries 2` revisions. It does not promise 50 accepted tasks.
A single additional novelty worker is used. Model credentials must already be exported in the calling shell;
workers inherit `SSL_CERT_FILE` and the API environment without storing credentials in the run files.
The default models remain Claude Sonnet for concepts/generation/novelty and MiMo for the two-stage judge.
`--generator-model`, `--judge-model`, and `--novelty-model` can override these for a new run.

To leave it running in screen, from this repository directory:

```bash
SSL_CERT_FILE=/tmp/taskgen-ca.pem screen -dmS taskgen-50 /bin/zsh -c \
  'caffeinate -i .venv/bin/python -u parallel_run.py --n 50 --generation-workers 3 --judge-workers 2 >> parallel-50.log 2>&1'
```

On Linux omit `caffeinate -i`. On macOS keep the machine plugged in and the lid open.

The command prints its new `runs/<timestamp>-parallel-<suffix>` folder. Resume that folder with:

```bash
SSL_CERT_FILE=/tmp/taskgen-ca.pem .venv/bin/python -u parallel_run.py \
  --resume runs/<timestamp>-parallel-<suffix> --generation-workers 3 --judge-workers 2
```

Resume uses the original run's candidate count, models, seed and revision settings. Worker counts can change.
It handles parallel runs only; use `rejudge.py` for previously generated serial-run drafts.
Ctrl-C stops workers and leaves durable state. Incomplete jobs are reclaimed on resume; unjudged drafts are judged
again without regenerating them. A completed specification review is reused if the difficulty review was interrupted,
provided the request/model/guidelines match. Finished candidates are not regenerated or rejudged.

### Queue, novelty and outputs

- SQLite `queue.sqlite` is the authoritative queue. A coordinator claims jobs and commits transitions; child processes
  do not edit it. A shared domain lock prevents new parallel, serial-generation or rejudge commands from concurrently
  writing the ledger. Stop older running processes that predate the lock before starting this batch.
- Up to three generation workers feed saved drafts to two judge workers immediately, without waiting for the whole
  batch. Each judge job runs the specification and difficulty stages sequentially, with 60k output tokens per stage.
- Concept novelty is checked against accepted past tasks and concepts already reserved in this batch. Comparison
  uses a deterministic lexical shortlist (up to 12 full engineering summaries), followed by an LLM assessment of
  physical function, design decisions, operating regimes and failure mechanism. It is a heuristic, not exhaustive
  semantic search across every past task. Legacy `runs/*/drafts/*/draft.json` and accepted `tasks/` are included.
- Final novelty admission is serialized and compares against the latest accepted tasks, preventing two concurrent
  duplicates from both being accepted. Concrete duplicates require a new concept within the slot's revision budget.
  Unclear novelty or exhausted review transport retries preserve the candidate as `unjudged`, with evidence.
- `tasks/`, `entries/`, `summary.json`, `cost.json`, and the domain ledger are written only by the coordinator.
  Accepted folders also include `novelty.json`. Stage 1 remains a separate `run.py --skillcheck <run>` step.
- `work/<slot>/attempt-<n>/<stage>/worker.log` captures each worker's streaming progress. Generator requests, raw
  generated drafts, completed judge-stage checkpoints and result receipts are kept beside the logs. `history/`
  retains finished worker receipts, including failed/rejected attempts. `unjudged/` preserves complete drafts.
- API usage has unique call IDs so cached/resumed judge-stage usage is counted once. Completed-call receipts are
  durable, but a provider call killed before returning usage can still be billed without appearing in local totals.
- The scheduler bounds concurrency, not provider rate limits or total spend. Use fewer workers if throttled. API
  failures do not get converted into engineering rejection or blind task regeneration during judging.

Implementation includes offline tests for queue claims/recovery, usage deduplication, novelty admission/retries,
checkpoint reuse and API failure preservation. These changes have **not been executed or tested in a live batch**.

### Dollar budget and deferring novelty

Resume the same batch without novelty calls and with a cumulative $50 local scheduling budget:

```bash
SSL_CERT_FILE=/tmp/taskgen-ca.pem .venv/bin/python -u parallel_run.py \
  --resume runs/<parallel-run> --budget-usd 50 --skip-novelty \
  --generation-workers 3 --judge-workers 2
```

These two flags can override the saved run settings. Previous spend is included; this is not $50 of additional
spending. Concepts/drafts/reviews already saved are reused. Queued novelty checks are bypassed, and passing judged
tasks are packaged with `novelty.json` marked deferred. No novelty worker or novelty model call is started. The
separate later novelty/ranking/NMS workflow is not implemented by this flag.

`budget.json` reports recorded usage, reserved allowance, and their total. Before dispatch, the coordinator reserves
$1 per concept, $4 per draft (including wording edits), and $1 per split-judge job. Successful priced calls release
the allowance in favor of actual usage. Failed or interrupted calls retain their allowance conservatively, including
calls that were in flight before budget tracking was introduced. When the remaining allowance cannot fund another
job, dispatch pauses and pending jobs stay in SQLite; in-flight jobs finish. Resume can raise the budget later.

This is a local scheduling cap with conservative estimates, **not a provider-enforced billing guarantee**. Pricing,
SDK retries, and usage lost during interruption can differ from the reserved amounts. Use provider/account-side
spending controls too if an exact billing ceiling is essential. The runner does not terminate paid calls merely
because a completed call reports more usage than estimated.

## Publish reviewed tasks to Google Drive / Sheets

`publish_drive.py` is a separate post-processing step. It does not run generation,
Stage 1, or novelty calls, and does not interfere with an active generation run.
It publishes only Stage 1 `advance` tasks with a matching `draft_digest` and a
current novelty approval. Stage 1 rank is an idea-value rank, not measured model
difficulty; feasibility text is a judge assessment, not a passing reference claim.

Install the optional client dependency:

```bash
uv sync --extra publish
```

Authenticate locally using Google Application Default Credentials (for example
user OAuth ADC with Drive and Sheets scopes), or pass `--credentials PATH` to an
OAuth authorized-user JSON file. `GOOGLE_APPLICATION_CREDENTIALS` can instead
point to a service-account credential file. Keep credential files outside the
repository. Enable Drive API and Sheets API for the credential project. The
identity must have edit access to both destinations. A service account generally
needs a Shared Drive destination or domain delegation: ordinary shared My Drive
folders do not provide it personal file-storage quota. User OAuth is the simplest
option for a personal Drive. The ChatGPT connector's login is not local pipeline
authentication. Existing `SSL_CERT_FILE` settings also apply to the requests client.

For the later batch novelty/NMS pass, save its selection as JSON:

```json
{
  "selected": ["02-example-task"],
  "draft_digests": {"02-example-task": "SHA256_OF_CURRENT_DRAFT"}
}
```

These digests use `evidence.draft_digest(meta['draft'])`; the novelty analysis must
write them when it makes the selection. A selected ID is a task folder basename.
Alternatively a task's `novelty.json` can contain `verdict: "distinct"` and the
current `draft_digest`. Legacy/deferred novelty records without a digest require
batch selection. The publisher does not manufacture novelty approval.

Preview locally (no credentials or API calls):

```bash
.venv/bin/python publish_drive.py runs/YOUR_RUN --selection selection.json
```

Publish using the default destination folder/spreadsheet supplied in this session:

```bash
SSL_CERT_FILE=/tmp/taskgen-ca.pem .venv/bin/python publish_drive.py runs/YOUR_RUN \
  --selection selection.json --apply
```

Use `--folder ID --spreadsheet ID --tab TITLE` for other destinations. The default
worksheet is `sheetId=0`. An existing nonempty sheet must have exactly the expected
headers; otherwise publishing stops before uploads. Use a dedicated empty tab to
preserve existing data. Headers are: Task ID, Task, Description, Domain, Stage 1,
Feasibility, Difficulty / value, Main concern, Novelty, Task folder. Strings are
written with RAW semantics to avoid interpreting task text as spreadsheet formulas.

The uploader preserves the task directory structure for instructions, handbook,
inputs and reviews. It uploads neither logs nor credentials nor arbitrary working
files. Existing tagged task folders/files are updated, and spreadsheet rows are
upserted by run/task ID. Drive application properties provide recovery even when
an upload succeeded before the process died. Progress is recorded in the run's
`publication.json`; rerun the same command after failures. Task content changes
require current reviews but reuse the same destination folder and row. Removed
assets are not deleted remotely; use a new task ID for a separately versioned
release. Run one publisher per checkout; simultaneous publishers from separate
machines targeting the same sheet are not supported. Transient read/update errors
retry with backoff; ambiguous create failures stop for a safe rerun.

No live Google upload has been tested yet. Destination access and local credentials
must be established before the first `--apply`; the connected account previously
could read the folder but could not access the specified spreadsheet.

## Private Vercel task gallery

A standalone alternative to Google Drive/Sheets is in [`task-gallery/`](task-gallery/README.md).
It uses a server-checked shared password and signed, eight-hour session cookies.
Task pages, search results and ZIP downloads require access; content is never served
from a public static folder. This is a shared-password gate, not WebAuthn passkeys.

After Stage 1 and batch novelty, export the selected snapshot:

```bash
.venv/bin/python export_gallery.py \
  runs/20261023-162428-run \
  runs/20261023-183125-run \
  runs/20261024-102325-parallel-0ff97d
```

Commit `task-gallery/gallery_data/`, import the repository in Vercel with Root
Directory **task-gallery**, and set only `GALLERY_PASSWORD`
in Vercel's environment settings. See the gallery README for setup, local preview,
password rotation and validation details. No Google credentials are involved.
