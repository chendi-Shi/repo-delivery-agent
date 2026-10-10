# First Docker delivery proof: retained failure

This is a real model attempt, not a scripted patch or protocol fixture. Run
[37916573901](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/37916573901)
used source commit `276c6cede818a43fc0e1c3bb69f3670c10003035`, the original public
Requests task `psf__requests-2931`, and `qwen2.5-coder:7b`. The archive SHA256 and
source binding are in `artifact-provenance.json`.

## Observed outcome

- 24 actual provider requests; 46,374 input and 1,107 output tokens, all usage
  reported by the provider. These values come from `model-runs/*/evidence.json`.
- 636.38 seconds measured for the repository run, including its internal checks;
  this is not a provider-only latency measurement.
- All 24 executed actions were the same invalid Skill read targeting a repository
  source path. Each returned code 2 and the frozen-Skill-resource error.
- Empty patch, `LimitsExceeded`, no submission, public delivery success **0/1**.
  The independent clean-checkout verifier passed existing text/form checks and
  failed the binary-body contract with the original `UnicodeDecodeError`.
- Official prediction export contains zero submissions and preserves the one
  failed attempt. No official resolved score exists for this attempt.

The top-level runner summary incorrectly left `model_calls` and `usage` null even
though the hash-bound evidence contains the actual measurements. Those original
artifact bytes are retained. Later runner code fixes this extraction; it does
not rewrite this historical result.

## Diagnosis and resulting engineering changes

The action log directly demonstrates confusion between Skill resources and
repository source. The old error did not describe how to recover. Tool guidance
and the error now explain the distinction and give a valid Skill discovery read;
Docker guidance demonstrates a normal shell source read.

Code inspection additionally identified a deterministic feedback loop: after the
recent context window contains only repeated identical command/error pairs, the
native JSON provider receives identical messages with fixed temperature and seed.
A protocol regression reproduces that request repetition; it is a model-free
diagnostic, not another performance attempt. The context policy now adds bounded
repetition feedback derived from actual execution metadata and persisted history.
It preserves raw evidence, does not execute a replacement action, and includes the
feedback in the context budget.

These changes address a measured failure mode. Their impact on successful model
delivery must be measured in subsequent real runs. This failed run remains part
of the project history and is not a success example.
