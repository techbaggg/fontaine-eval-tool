"""
Classify tests into F2P / P2P / P2F / F2F from three stage maps.

Semantics mirror common 3-run PR evaluation (base / base+tests from head / base+full patch).
"""

from __future__ import annotations

PASSED_STATUSES = frozenset({"PASSED", "XFAIL"})
FAILED_STATUSES = frozenset({"FAILED", "ERROR"})


def classify_f2p_p2p(
    tests_base: dict[str, str],
    tests_before: dict[str, str],
    tests_after: dict[str, str],
    *,
    has_new_test_file: bool = False,
) -> dict[str, list[str]]:
    """
    ``tests_*`` map test id -> ``PASSED`` | ``FAILED`` | ``SKIPPED``.

    Returns keys ``FAIL_TO_PASS``, ``PASS_TO_PASS``, ``PASS_TO_FAIL``, ``FAIL_TO_FAIL``.
    """
    result: dict[str, list[str]] = {
        "FAIL_TO_PASS": [],
        "PASS_TO_PASS": [],
        "PASS_TO_FAIL": [],
        "FAIL_TO_FAIL": [],
    }

    has_mixed_before = any(s in PASSED_STATUSES for s in tests_before.values()) and any(
        s in FAILED_STATUSES for s in tests_before.values()
    )

    if has_new_test_file or not has_mixed_before:
        base_passing = {t for t, s in tests_base.items() if s in PASSED_STATUSES}
        after_passing = {t for t, s in tests_after.items() if s in PASSED_STATUSES}

        fail_to_pass = [t for t in after_passing if t not in base_passing]
        pass_to_pass = [t for t in after_passing if t in base_passing]

        before_passing = {t for t, s in tests_before.items() if s in PASSED_STATUSES}
        reclassify_to_p2p = [t for t in fail_to_pass if t in before_passing]
        if reclassify_to_p2p:
            fail_to_pass = [t for t in fail_to_pass if t not in reclassify_to_p2p]
            seen = set(pass_to_pass)
            for t in reclassify_to_p2p:
                if t not in seen:
                    pass_to_pass.append(t)
                    seen.add(t)

        before_failing = {t for t, s in tests_before.items() if s in FAILED_STATUSES}
        reclassify_to_f2p = [t for t in pass_to_pass if t in before_failing]
        if reclassify_to_f2p:
            pass_to_pass = [t for t in pass_to_pass if t not in reclassify_to_f2p]
            seen = set(fail_to_pass)
            for t in reclassify_to_f2p:
                if t not in seen:
                    fail_to_pass.append(t)
                    seen.add(t)

        result["FAIL_TO_PASS"] = fail_to_pass
        result["PASS_TO_PASS"] = pass_to_pass
    else:
        all_tests = set(tests_before.keys()) | set(tests_after.keys())
        for test in all_tests:
            status_before = tests_before.get(test)
            status_after = tests_after.get(test)

            if status_before in FAILED_STATUSES and status_after in PASSED_STATUSES:
                result["FAIL_TO_PASS"].append(test)
            elif status_before in PASSED_STATUSES and status_after in PASSED_STATUSES:
                result["PASS_TO_PASS"].append(test)
            elif status_before in PASSED_STATUSES and status_after in FAILED_STATUSES:
                result["PASS_TO_FAIL"].append(test)
            elif status_before in FAILED_STATUSES and status_after in FAILED_STATUSES:
                result["FAIL_TO_FAIL"].append(test)

    return result
