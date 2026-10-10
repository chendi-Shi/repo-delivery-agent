"""Atomic publication retries only a saved rename, never its producing action."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from delivery_agent import repository


class AtomicJsonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="atomic-json-")
        self.root = Path(self.temporary.name)
        self.target = self.root / "summary.json"
        self.pending = self.root / "summary.json.tmp"
        self.original = b'{"state":"original"}\n'
        self.target.write_bytes(self.original)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def windows_error(code):
        failure = PermissionError(13, "controlled replacement conflict")
        failure.winerror = code
        return failure

    def test_normal_publication_preserves_unicode_and_consumes_temporary(self):
        repository.atomic_json(self.target, {"state": "已保存", "count": 2})
        self.assertEqual(json.loads(self.target.read_bytes()), {"state": "已保存", "count": 2})
        self.assertIn("已保存", self.target.read_text(encoding="utf-8"))
        self.assertFalse(self.pending.exists())

    def test_each_transient_windows_error_reuses_exact_serialized_bytes(self):
        real_replace = repository.os.replace
        real_dumps = json.dumps
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                self.target.write_bytes(self.original)
                value = {"state": "new", "count": 2}
                expected = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").replace(
                    "\n", repository.os.linesep).encode()
                attempts = []

                def replace(source, target):
                    attempts.append((source, target))
                    self.assertEqual((source, target), (self.pending, self.target))
                    self.assertEqual(source.read_bytes(), expected)
                    self.assertEqual(target.read_bytes(), self.original)
                    # A retry that reruns serialization would publish this
                    # later mutation instead of the already prepared snapshot.
                    value["state"] = "changed after serialization"
                    if len(attempts) < 3:
                        raise self.windows_error(code)
                    real_replace(source, target)

                with mock.patch.object(repository, "_IS_WINDOWS", True), \
                     mock.patch.object(repository.os, "replace", side_effect=replace) as rename, \
                     mock.patch.object(repository.time, "sleep") as sleep, \
                     mock.patch.object(repository.json, "dumps", wraps=real_dumps) as dumps:
                    repository.atomic_json(self.target, value)
                self.assertEqual(self.target.read_bytes(), expected)
                self.assertFalse(self.pending.exists())
                self.assertEqual(rename.call_count, 3)
                self.assertEqual(dumps.call_count, 1)
                self.assertEqual(sleep.call_args_list, [mock.call(0.02), mock.call(0.05)])

    def test_last_bounded_attempt_can_publish_successfully(self):
        real_replace = repository.os.replace
        attempts = []

        def replace(source, target):
            attempts.append((source, target))
            if len(attempts) <= len(repository._REPLACE_RETRY_DELAYS):
                raise self.windows_error(32)
            real_replace(source, target)

        with mock.patch.object(repository, "_IS_WINDOWS", True), \
             mock.patch.object(repository.os, "replace", side_effect=replace), \
             mock.patch.object(repository.time, "sleep") as sleep:
            repository.atomic_json(self.target, {"state": "new"})
        self.assertEqual(len(attempts), 6)
        self.assertEqual(sleep.call_args_list, [mock.call(delay) for delay in repository._REPLACE_RETRY_DELAYS])
        self.assertEqual(json.loads(self.target.read_bytes()), {"state": "new"})

    def test_exhaustion_preserves_original_target_pending_bytes_and_original_exception(self):
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                failure = self.windows_error(code)
                with mock.patch.object(repository, "_IS_WINDOWS", True), \
                     mock.patch.object(repository.os, "replace", side_effect=failure) as rename, \
                     mock.patch.object(repository.time, "sleep") as sleep:
                    with self.assertRaises(PermissionError) as caught:
                        repository.atomic_json(self.target, {"state": "new", "code": code})
                self.assertIs(caught.exception, failure)
                self.assertEqual(rename.call_count, 6)
                self.assertEqual(sleep.call_args_list, [mock.call(delay) for delay in repository._REPLACE_RETRY_DELAYS])
                self.assertEqual(self.target.read_bytes(), self.original)
                self.assertEqual(json.loads(self.pending.read_bytes()), {"state": "new", "code": code})

    def test_other_errors_propagate_immediately_even_on_windows(self):
        for code in (None, 2, 87, 123):
            with self.subTest(winerror=code):
                failure = self.windows_error(code)
                with mock.patch.object(repository, "_IS_WINDOWS", True), \
                     mock.patch.object(repository.os, "replace", side_effect=failure) as rename, \
                     mock.patch.object(repository.time, "sleep") as sleep:
                    with self.assertRaises(PermissionError) as caught:
                        repository.atomic_json(self.target, {"state": "new"})
                self.assertIs(caught.exception, failure)
                self.assertEqual(rename.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(self.target.read_bytes(), self.original)
                self.assertTrue(self.pending.exists())

    def test_non_windows_does_not_retry_matching_error_numbers(self):
        failure = self.windows_error(5)
        with mock.patch.object(repository, "_IS_WINDOWS", False), \
             mock.patch.object(repository.os, "replace", side_effect=failure) as rename, \
             mock.patch.object(repository.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                repository.atomic_json(self.target, {"state": "new"})
        rename.assert_called_once_with(self.pending, self.target)
        sleep.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_temporary_write_failure_is_not_retried_or_renamed(self):
        failure = self.windows_error(5)
        with mock.patch.object(Path, "write_text", side_effect=failure) as write, \
             mock.patch.object(repository.os, "replace") as rename, \
             mock.patch.object(repository.time, "sleep") as sleep:
            with self.assertRaises(PermissionError) as caught:
                repository.atomic_json(self.target, {"state": "new"})
        self.assertIs(caught.exception, failure)
        write.assert_called_once()
        rename.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
