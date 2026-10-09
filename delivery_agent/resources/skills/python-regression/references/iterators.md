One-shot iterator checks

1. Construct a generator whose values differ on a second pass, or whose iteration count is observable. Verify the requested operation consumes it once.
2. Use lists or dictionaries as elements if the contract depends on equality and does not require hashing. An implementation that constructs a set silently narrows that contract.
3. A test for adjacent runs should separate equal adjacent values from equal values separated by another value. For example, [1, 1, 2, 1] has three runs, while two distinct values occur globally.
4. Confirm empty-input behavior explicitly. Avoid changing unrelated exception types or consuming extra items solely to inspect an input.

Use only the checks relevant to the task's documented behavior.
