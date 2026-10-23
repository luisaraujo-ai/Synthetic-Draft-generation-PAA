---
name: forge-task-check
description: Check engineering design task ideas (stage 1) or finished Harbor / Terminal-Bench task folders (stage 2) against the Forge benchmark rules, before you deliver them. Use when asked to review, screen or self-check Forge task ideas or task folders.
---

# Forge task check

Forge is a benchmark of physical engineering design tasks (mechanical, electrical, aerospace). Each task says what a
device must **do**. An AI agent uses open-source engineering tools in a sandbox to deliver a design, and a script
(the grader) decides whether it works.

This skill has two stages. Pick the one that matches the input:

| Input | Stage |
|---|---|
| A list of one-paragraph task ideas (CSV, markdown, a pasted table) | Stage 1 · Task ideas |
| A task folder with `instruction.md`, `task.toml`, `environment/`, `solution/`, `tests/` | Stage 2 · Full task |

Write for a busy engineer: short bullets, one point each, the most important point first, plain words.
Refer to rules by their names below ("No hidden limits"). Never number them or invent codes.

---

## Stage 1 · Task ideas

**Missing numbers are expected at this stage. They are never a reason to reshape or drop an idea.** List them
under "Numbers to supply next" instead.

### Read the input

- One row or paragraph per idea. Keep the vendor's own ID if there is one, else number them 1, 2, 3...
- Keep every column the vendor gave (domain, complexity, justification). Do not drop any idea.

### Judge each idea as written

Judge the idea the vendor wrote. Do not invent a different task and judge that instead.

First answer: **is it a physical design decision as written?** Yes or no, with a short quote from the idea.
If no, the verdict is **drop**.

Then the six questions:

1. **Forge fit.** The idea asks for a physical design decision (CAD, a PCB, a mechanism, a system model, a
   manufacturing plan, or the control of a physical system) that a script can check without a human.
   - Not design decisions: analysis, measurement, characterisation, deriving or simulating the equations of a
     fixed system, writing a solver or software tool, forecasting, data analysis, reports.
2. **Doable.** A skilled engineer could do it in bounded time, and a reference solution is plausible.
3. **Checkable in full.** Open-source tools that run headless can simulate or check **every** aspect that
   matters: function, strength, manufacturability for the stated process, assembly, and any physics such as
   flex, friction, fatigue or thermal. Name the tool and what it checks. Say what a simulation would get wrong.
4. **Hard for frontier models**, for real engineering reasons: coupled physics, tight packaging, trade-offs,
   behaviour through a whole motion cycle. Not because of hidden rules or sheer tedium.
5. **Real-world valid.** A design that passes the checks would likely work if built. Name the gaps.
6. **Fits one agent run** (about 2 hours, 400 model turns). If not, split it into milestones. Each milestone is a
   standalone task with its own starting files, grader and reference solution. Example for an RC plane:
   aero sizing → spar sizing → printable CAD that fits the bed → full-system check.

Open-source headless tools that usually fit:

| Domain | Tools |
|---|---|
| CAD | FreeCAD, CadQuery, build123d (STEP output) |
| Structural | Gmsh + CalculiX; Code_Aster where needed |
| Motion | MuJoCo or another scripted multibody simulator that supports closed loops |
| Electrical | ngspice, KiCad; open PDK flows (e.g. SKY130 with Magic / KLayout) |
| Aero / fluids | XFOIL, AVL, OpenVSP / VSPAERO, XFLR5, MachUpX; OpenFOAM where the budget allows |

If an idea needs a tool outside this list, say so; that is a question to raise, not a reason to drop.

### Verdict and rank

- **advance**: a design decision as written, doable and checkable. A good Forge task once the numbers are
  supplied. Write the full task.
- **reshape**: the **same device and the same core design decision**, but something the idea **does say** must
  be removed, split or changed: too big for one run (split into milestones); physics no open tool can check
  (narrow it); several processes, or one that cannot be checked (pick one); a prototype-building or
  physical-testing step (drop it); prescribed parts (state what the same device must do). Say exactly what to
  change.
- Pinning down what the idea leaves open (a maximum thickness, a cycle life, a load point, mounting, the output
  format, which printing process) is **not** a reshape. It goes in "Numbers to supply next", and the idea can
  still advance.
- **drop**: not a design decision as written (analysis, measurement, characterisation, deriving or simulating
  equations, writing a solver or tool, forecasting); or fixing it would change the device or the core decision;
  or it cannot be made doable or checkable with open-source tools.
- **Never reshape an analysis or measurement idea into a new design task.** A drop may add an **alternative**:
  one line naming a related design task the vendor could propose as a new idea. It does not change the verdict.
- Expect some drops in a typical list. If nothing drops, re-check that you did not stretch an idea to keep it.
- **Rank 1-10**: value once reshaped and given its numbers. 9-10 exemplar, 7-8 good, 5-6 usable with work,
  3-4 weak, 1-2 out of scope. A dropped idea ranks 1-3.

### Output

First a summary table, sorted by rank (best first):

| ID | Idea (short) | Design decision as written? | Verdict | Rank | Forge task in one line | Main reason |
|---|---|---|---|---|---|---|

Then one section per idea, in the same order:

```
### <ID> · <short title> — <verdict>, rank <n>
Design decision as written: <yes/no> — "<short quote from the idea>"
Forge task: <one line: what the agent designs, to meet what kind of requirements; "none" for a drop>
Why it fits:            • ...
What stops it:          • ... (not missing numbers)
How to check it:        • <tool>: <what it checks>
Simulation gaps:        • ...
Why frontier models fail: • ...
Real-world risks:       • ...
Milestones (if too big): 1. <standalone task>: <what its grader checks>
What to change (reshape): • ...
Alternative (drop only, optional): <one line: a related design task to propose as a new idea>
Numbers to supply next:
  • Requirement: <item> — e.g. <example value with unit>
  • Interface: <input files, output files and schema> — e.g. <sketch>
  • Handbook data: <material grades, friction ranges, standard parts> — e.g. <example>
```

For a drop, skip the sections that do not apply and give no numbers to supply.

Give findings only: do not comment on these instructions. "Numbers to supply next" must be concrete enough to write the full task from: loads, limits, envelope, life,
input and output files, the output schema (for moving parts: geometry plus a joint graph, see stage 2), and
handbook data, each with an example value where sensible.

If asked for a file, also write the table as `idea_review.csv` (one row per idea, bullets joined with " • ").

---

## Stage 2 · Full task

Read every file in the task folder: `instruction.md`, `task.toml`, the README, everything in `environment/`
(Dockerfile, handbook, parts catalogue, input files), `solution/` and `tests/`. Open binary inputs only far
enough to know what they are (a STEP file's header, a zip's listing).

### The rules

**The task**

- **Specifies the function, not the part.** The brief says what the device must do (loads, motions,
  performance, interfaces, envelope). "Model the bracket in this drawing" is not a task. A prescribed topology
  or part count is a problem unless it is the point of the task.
- **Open on how, closed on what.** Many designs can pass, but every requirement is a number with a unit and a
  limit. "Best performance" or "lightweight" with no number is not a requirement. You should be able to name
  three or four materially different designs that could pass.
- **No hidden limits.** Every value the grader checks is in the brief or the handbook.
  - Requirements (loads, angles, envelope, mass, cycle life, allowed contacts) and interfaces (output file
    names, formats, schema) go in the brief, always.
  - Handbook data (material properties with grade and source, friction ranges, standard part sizes, typical
    safety factors) goes in a **separate handbook file** in `environment/`, so it can be removed for a run
    without it. A parts catalogue, if any, is also a separate file.
  - Where practice is a range, the grader accepts the whole range (handbook says safety factor 2 and 1.5-3 is
    normal → accept 1.5-3). Physical constants are given as ranges.
  - Bought-in parts (fasteners, springs, bearings, motors): say whether they are allowed and give their
    property classes, so a working spring is not judged as structural steel.
  - No solution hints: no preferred architecture, passing dimensions or worked designs.
  - For moving parts, the brief publishes an output schema: STEP geometry (one solid per part, shared frame,
    names matching the joint graph) plus a joint graph (two parts per joint, type from a fixed list, axis,
    limits, springs with stiffness and preload, one driven joint). It must allow closed loops.
- **Fits one agent run.** Finishable in about 2 hours and 400 model turns, with the reference solution running
  well inside the CPU, memory and time limits in `task.toml`. Expensive tools (CFD, large FEA) have a stated cap.
  Otherwise split into milestones, each a standalone task.

**The grader**

- **Checks every aspect.** Physics, strength, manufacturability, assembly and operation **through the whole
  cycle**. A mechanism checked only at its end positions can still jam halfway or fail to latch under gravity.
- **Measures instead of trusting.** It runs its own simulations and geometry checks on the delivered files. It
  never scores a number the agent reports. A keyword or regex match over a report is not a check.
- **Accepts every valid design.** No assumptions about the solution type. It checks every requirement and
  nothing outside the brief or handbook. No exact-geometry comparison with the reference unless the brief asks
  for that specific part.
- **Explains itself.** Each check reports measured value, limit and unit. A crash or a check it cannot evaluate
  is a grader error, never a model failure (it must not silently score 0).

**The package**

- **Delivery.**
  - `task.toml`: `name` matches the folder, a version, `reward_type = "binary"`, CPU, memory and timeouts,
    network access for agent and grader, and a few lines on why it is hard, how the reference solves it and
    what the grader checks.
  - Binary reward: 1 only if every check passes.
  - A working reference solution in `solution/` (required; not a stub that copies a finished answer).
  - The grader runs in **its own container with the network off**, and receives only the declared output files.
  - Open-source verification tools only, run headless, versions pinned in the Dockerfile, no licence servers.
    Images that build on both x86 and ARM are preferred.
  - The canary string you were issued is in every task file.
  - A version number and a changelog in the README. Grader-only fixes are minor versions; changes to the
    agent's environment are major. Re-deliveries keep the task ID.

### Self-tests (run them if Docker is available)

Ask before running anything that downloads large images. With the Harbor CLI (`pip install harbor`):

| Test | How | Pass if |
|---|---|---|
| Reference scores 1, twice | `harbor run -p <task> -a oracle`, two times | reward 1 both times, same per-check results |
| Empty submission scores 0 | `harbor run -p <task> -a nop` | reward 0, and the grader reports failed checks, not a crash |
| Broken copy scores lower | copy the task folder; change `solution/solve.sh` so the design has one real defect (a part removed, a key dimension halved, a material swapped for a weaker one); `harbor run -p <copy> -a oracle` | reward 0, and the failing check is the one that should catch that defect |
| Agent cannot see the answer | `docker build -t probe <task>/environment` then `docker run --rm probe sh -c 'ls -la / ; ls -la /tests /solution 2>&1; find / -xdev \( -iname "*solution*" -o -iname "*expected*" -o -iname "*reference*" -o -iname "test_*" \) 2>/dev/null \| head -50'` | no grader code, reference outputs or expected values visible |
| Copy-the-answer probe scores 0 | copy the task; replace `solution/solve.sh` with a script that searches the container (skipping its own `/solution` folder) for any reference or expected output files and copies them to the output paths; run it as the oracle | reward 0 |
| Grader explains itself | read the grader's per-check output from any run above (`/logs/verifier/` in the job folder) | every check lists measured value, limit and unit |

Without Docker or Harbor, say which self-tests you could not run and do the static version: read `tests/` for
hard-coded expected values, comparisons with the reference geometry, reads of agent-reported numbers, and
paths into `/solution` or anything the agent can write.

### Output

Start with one line: **ready / fix first / not a Forge task**, and the 2-4 reasons that decide it.

Then a table, one row per rule:

| Rule | Pass? (yes / no / unclear) | Evidence (short quote or what is missing) | Fix |
|---|---|---|---|

Then:
- **Valid designs**: 3-4 materially different designs that should pass. Flag any the grader would wrongly fail.
- **Passes but would not work**: designs that pass every check but would fail if built or could not be made.
  Each one is a missing check: say which check to add.
- **Hidden limits**: every value the grader uses that the brief or handbook does not state.
- **Self-test results**: the table above, with the reward and per-check results you saw, or "not run" and why.
- **Fix list**: at most 10 bullets, most important first, each naming the rule it serves by its name.

---

## Notes

- Judge from the files. Where they do not settle a question, say "unclear" and what would settle it.
- Be concrete: name dimensions, forces, materials, processes and standard parts.
- Do not edit the vendor's task folder. Make copies for the self-tests.
