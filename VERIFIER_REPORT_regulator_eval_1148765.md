# Report: regulator task, Astra eval 1148765 (and 1148510)

## What failed
Both evals scored no reward: 7 static checks passed, 11 `regulator.transient_metric` checks returned `grader_error`, kforge exited non-zero, `test.sh` aborted before writing reward.txt, Harbor raised RewardFileNotFoundError.
- 1148510: `Incomplete transient` (ngspice aborted "Timestep too small" at ~10 ns in a warm-start case).
- 1148765: `ngspice exceeded 45 s` on a warm-start case (vs=32, in0=32, lc=0.7, capacitors 0.8).
Astra's netlists were sound; the task grader failed to simulate them.

## Root causes (verifier / task library)
1. **Fragile warm-start initial conditions.** `grade.simulate` sets only `cx_int1` (cap charge) for precharged caps under `uic`; the nodes (vin, ESR nodes) start at 0 V. A cap at 32 V behind a 5 mOhm ESR makes a kA-scale impulse at t=0. Whether ngspice integrates through is chaotic w.r.t. options, step, and design (any design with a damper cap on vin triggers it). Setting node ICs consistently (vin, cable, vout, ESR nodes) was tried and made the *reference* fail (singular matrix at vout), so the fix is not a one-liner; it needs a proper IC scheme.
2. **One failed simulation kills the whole suite.** `transient_metric` caches the exception and every row becomes grader_error, so one non-convergent case voids 11 checks and the reward.
3. **Timeout bypassed fallbacks / no diagnostics.** `TimeoutExpired` raised immediately; error text carried no solver log or case info for "Incomplete transient".
4. **Reference solution violated R3.** solve.sh (Rz=10k) gave 159 mV ripple at vs=32/50 kHz/corner vs the 150 mV limit (8 failing sims). The task was unsatisfiable by its own reference.
5. **Runtime.** Full grade takes ~4.5 min when no retries are needed and ~24 min (emulated amd64) when warm cases need retries; verifier timeout was 1800 s.
6. Harness: `test.sh` should write reward.txt = 0 / an infra marker even when kforge exits on grader_error, or the infra failure should be explicit.

## Fixes made in the task (tests/grade.py, solution/solve.sh, task.toml)
- `simulate` now retries through an ordered list (`ATTEMPTS`: options x step divisor): nominal gear; gear+`trtol=1 itl1=1000`; step/2,/4,/3,/10; trap; two looser last-resort settings. 4 s cap per non-final attempt, 45 s on the last. Timeouts and truncated waveforms are retried. Tolerances stay at nominal for all but the last two.
- If all attempts fail it raises a VerifierError with the case, last stop time, and ngspice log tail (still a grader error, not a design failure).
- solve.sh Rz 10k -> 5.6k: reference now passes all 1038 sims with 0 failures.
- Verifier timeout 1800 -> 3600 s.
- Verified in the grader container (ngspice-47): Astra 1148765 netlist, 1038 sims, 0 failed checks (1442 s); reference, 1038 sims, 0 failed checks.

## To patch in the verifier library
- Replace the `uic` + cx_int1-only warm start with a fully consistent initial state (or a short settling phase), then drop the retry grid.
- Isolate per-case failures: report them as BLOCKED for the affected rows only; do not cache one exception for all rows.
- Always write reward/infra status from test.sh; keep the failed deck and ngspice log under /logs/verifier.
- Add a regression test: a design with a damper cap on vin must complete every warm case (in0=32, lc=0.7/1.3, caps 0.8/1.2).
- Add a CI check that the reference solution passes the full suite.
