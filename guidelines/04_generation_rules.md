# Generation rules for this project

Strict rules for every generated task, on top of the documents above. They make the documents concrete where drafts
kept going wrong. The pipeline also checks rules 1–4 mechanically and regenerates drafts that break them.

## The brief says what, never how

1. **No software, tools or solver settings in the brief.** Do not name simulators, CAD packages, programming
   languages, libraries or versions, and do not give integration methods, time steps, solver options, meshing or
   probe settings. Name deliverables by engineering format ("a SPICE netlist", "a STEP assembly", "a JSON file with
   these fields"), not by the tool that produces them. Choosing and driving the tools is the solver's job. The only
   exception is when the method itself is what the task tests.
2. **No supplied test bench.** Supply the models of fixed external parts (the load, the source, the cable, the
   process models) and, for a repair task, the failing design. Do not supply the measurement bench, stimulus decks,
   probe points or scripts the grader uses. The solver builds its own verification. Where reasonable methods could
   give different pass or fail results, state the definition (what is measured, where, under which condition) in
   one line; never the procedure.
3. **No ungraded preferences.** Every sentence about performance is either a requirement with a number, a unit and
   a limit, or it is deleted. No "as low as possible", "lower X wins", "more margin is welcome", "ideally",
   "minimize" or "best". The reward is binary, so a preference the grader cannot score only distracts or baits.
4. **Short and human.** The brief is a request from a person, at most about 400 words including the requirement
   list. Context in two or three sentences, the requirements, the fixed interfaces and the deliverable files. No
   section of definitions longer than the requirements themselves; detail goes to the handbook. Never mention a
   benchmark, grader, reward, agent, sandbox, container or run budget, and do not put the canary string in the text
   (the packager adds it).
5. **Ask only for files the grader measures.** No datasheet of the solver's own claimed numbers, no report the grader
   does not read.

## The task must be able to break a strong model

6. **No public solution.** If a well-known public design meets every requirement (a standard-cell library cell, a
   reference design, an application-note circuit, a textbook example, a catalogue part), the task measures retrieval,
   not engineering. List the public designs a solver would reach for and the requirement each one fails. If you
   cannot name a failing requirement for each, change the requirements.
7. **The targeted mechanism must survive the solver's own checks.** The solver can simulate and measure its own
   design. Explain why an agent with the targeted limitation still fails even so. Valid reasons: the decisive
   condition is inside a declared range but away from the corners a solver naturally checks (held-out points); the
   failure only appears in an effect the solver must think to model (a state, a transition, a second operating
   mode, a coupled physics); or the binding trade-off leaves a narrow window that the limitation steers away from.
   "The agent might not check" is not a reason when the brief tells it exactly what to check.
8. **Binding, not generous.** The requirements should leave a narrow feasible window around the targeted
   trade-off, not let any sensible design pass with large margins. Mark the limits as estimates for the SME to
   calibrate.

9. **Asset grounding and SME-created prerequisites**

Never claim that a starter project, input file, device model, component library, reference design or other external artifact already exists unless it appears in the verified asset inventory.

A task may propose creating new engineering assets when they are necessary for a valuable physical design problem.

When required assets are not available:

Describe them explicitly as SME-created prerequisites, never as already supplied files.

Specify exactly what the SME must create, including physical models, interfaces, parameter ranges and required validation tests.

Estimate the engineering effort and classify the task as:

A: Self-contained or automatically generated assets.

B: Limited SME asset creation.

C: Significant custom infrastructure or uncertain model feasibility.

Keep uncreated assets out of the solver-facing brief until they have been built and validated.

Do not mark the task executable, packaged or ready for delivery until every prerequisite exists and passes validation.

Prefer existing validated model libraries and reusable verifier families when they preserve the targeted engineering challenge.

Do not simplify a genuinely difficult circuit-design problem into a shallow parameter-optimization task solely to eliminate asset dependencies.

A potentially valuable task with missing prerequisites may advance as a concept, but must remain blocked from execution until the prerequisites are complete.


## Engineering-depth requirements

**10. Explicit coupled physical model.** Before drafting the brief, identify the physical subsystems, state variables, governing relationships and coupling mechanisms. Explain which interactions must be represented to evaluate the design correctly. Include only physics relevant to its acceptance criteria. Do not invent unnecessary complexity.

**11. Independent performance and feasibility constraints.** Build a requirement matrix containing the measured physical quantity, operating condition, numerical limit, measurement location and verification approach. Distinguish actual device-performance constraints from requirements about numerical accuracy. Solver convergence and energy-accounting validity belong primarily in the verifier contract, unless numerical modeling is itself the task.

**12. Nonempty feasible region.** Before finalizing tight requirements, derive analytical bounds wherever possible. Check power conservation, voltage/current headroom, mechanical stress and strain, thermal budgets, component ratings, and other applicable necessary feasibility conditions. A candidate that violates any necessary physical bound must be revised or rejected. Analytical checks establish necessary conditions, not the existence of a passing design; numerical thresholds remain provisional until a passing reference is demonstrated.

**13. Multi-regime engineering challenge.** Identify the operating states, transients, nonlinearities, tolerances and coupled effects that determine success. At least one meaningful challenge should require coordinating multiple design decisions or physical effects. Describe the specific mechanism that a solver could overlook despite having access to its own verification tools. Do not increase difficulty merely by adding requirements.

**14. Verifiable acceptance contract.** Every solver-facing performance requirement must map to a measurable result from the submitted design. Specify the physical quantity and its definition, units, limits and applicable conditions. Record grader-specific algorithms, tolerances and numerical robustness checks separately. Never score self-reported simulation results when the grader can compute the quantities independently.


## Asset simplicity and automated task construction

**15. JSON-first engineering tasks.** Prefer engineering problems where the fixed inputs can be expressed in a generated `input.json` and the design decisions can be expressed in a `design.json`. The input should contain physical parameters, material properties, operating conditions, excitation profiles, boundary conditions, design-variable bounds, and any necessary reduced-order model coefficients. The output must contain genuine physical design decisions, not merely reported simulation results. Prefer this representation whenever it preserves the engineering capability being tested.

Do not force JSON when the task genuinely requires circuit-topology synthesis, PCB layout, CAD geometry, or another engineering artifact. A simplified parameter-optimization task must not be represented as equivalent to a full circuit or mechanical design task.

**16. Minimize custom external dependencies.** Strongly prefer tasks that can be evaluated from documented governing equations, generated numerical inputs, and reusable open-source verification tools. Avoid introducing custom SPICE libraries, process models, proprietary component catalogues, preexisting defective designs, or specialized simulation assets unless they are essential to the physical capability being tested.

Before selecting a task concept, compare whether the same engineering limitation can be tested using a self-contained, parameterized physical system. If it can, prefer that approach.

If a specialized model or existing design is essential, it must be supplied from a verified asset inventory or generated and validated by the workflow. Do not invent asset names, reference files or dependencies.

**17. Produce a complete asset specification.** For every proposed task, output an author-facing asset manifest containing all required input files, their sources, schemas, physical meanings, units, numerical ranges and generation methods.

For JSON-parameterized tasks, generate a complete proposed `input.json` with physically consistent values and a precise `design.json` schema. Identify the governing equations and verification methods needed to evaluate the submitted design.

Validate generated inputs for schema correctness, unit consistency, parameter bounds, physical consistency and compatibility with the intended verifier. A syntactically valid JSON file is not sufficient evidence that the engineering model is correct.

A task may be labeled **conceptually valid** before its assets are built, but it must not be labeled **executable or ready for delivery** until every required asset has been created, validated and made available at its declared path.

SSL_CERT_FILE=/tmp/taskgen-ca.pem uv run run.py \
  --run runs/20261023-094806-run \
  --step stage1 \
  --draft 00-sic-gate-drive-desat-corner-robust