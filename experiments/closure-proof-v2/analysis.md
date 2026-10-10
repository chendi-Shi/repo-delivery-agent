# Second real Docker proof: retained failure

Source commit `21f1f5f00b3294a79af3cfb6fcc19b7a4a5e8065`, [Actions run 38016237749](https://github.com/chendi-Shi/repo-delivery-agent/actions/runs/38016237749), artifact 11657051751. The downloaded ZIP matched GitHub's SHA256 `facdb4ea0a85189fc825c3ff5dfa8e17e8fdd549f51ad8907d49d7202e5928a6`. Original artifact files are unchanged.

The same Requests public task, 7B model digest, seed and budgets as the first proof were used. This is one engineering follow-up attempt, not a general success-rate estimate.

- Actual completed provider calls: 24; measured input/output tokens: 53,817 / 732. Usage is complete, with zero unknown requests.
- Repository run time: 1,624.684 seconds, including its internal verification. This is not end-to-end workflow time or pure inference time.
- Outcome: `stopped / LimitsExceeded`, no submission and an empty patch.
- A second clean checkout passed the legacy text/form check and failed the binary-body check with the original `UnicodeDecodeError`. Public delivery: 0/1; official result remains null because there was no prediction to grade.

The action audit shows one successful nonempty Skill read, followed by eleven reads at its exact end offset (`1192`) returning empty text and twelve invalid history searches. The searches literally copied `delivery-history search 'literal query' [limit 1..8]` from the tool guide. From action 5 onward, the two unchanged actions alternate until the step budget is exhausted. No repository source was read or edited.

The first repair made Skill syntax understandable, but its consecutive-failure feedback did not break a cycle containing both a successful empty read and an invalid search. The evidence motivates copyable tool examples, explicit end-of-resource guidance and feedback for repeated unchanged read-only cycles. These changes must be evaluated in fresh attempts; this failure is not reclassified as successful recovery.

The old runner's top-level `summary.json` still has null `model_calls` and `usage`. The measured values above come from the hash-bound `model-runs/.../evidence.json`, which retains the actual 24-call usage. Original bytes have not been rewritten to repair the historical summary.
