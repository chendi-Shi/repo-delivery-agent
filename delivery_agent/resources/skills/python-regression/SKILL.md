---
name: python-regression
description: Diagnose and fix Python repository defects with a regression test, including iterator exhaustion, exception behavior, and import compatibility.
---

Use the repository's existing test runner and nearby test style. Start from the smallest reproduction of the reported behavior; record its failure before editing implementation where possible. A test must assert observable behavior, including the relevant edge case, rather than duplicate the proposed implementation.

For iterator code, distinguish a reusable iterable from a one-shot generator and avoid accidental second traversal. Exercise empty inputs, unhashable values when equality is the intended operation, and exception propagation when these are part of the requirement. Read [references/iterators.md](references/iterators.md) when changing iterator consumption or equality semantics.

After fixing the narrow reproduction, run the relevant neighboring tests. Keep failures from unavailable dependencies separate from behavioral failures. Record exact commands and outcomes. Submission does not prove independent verification; the delivery harness reruns public checks in a fresh checkout.
