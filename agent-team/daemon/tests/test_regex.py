"""Regression tests for the dependency / PR-close link parsers in daemon.py.

These guard the contract documented in AGENTS.md ("Dependency parsing — regex
contract") and the broadened detection rules from issue #45 / #38.

Run directly with `python3 agent-team/daemon/tests/test_regex.py` — pytest is
intentionally NOT a runtime dependency of the daemon (see the daemon
Dockerfile comment near the dev-requirements block). The assertions below are
plain Python so the file works under both `python3 test_regex.py` and
`pytest agent-team/daemon/tests/`.
"""

import os
import sys
import types
import pathlib


# ── Test harness: stub external deps before importing daemon ──────────────
#
# daemon.py reads env vars at import time, constructs `docker.from_env()`,
# and imports `requests`. None of those are required for regex testing, so
# stub them out so this test file can run anywhere `python3` is installed,
# without pulling in the production dependency set.

os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GITHUB_REPO", "prasinus-automation/AutoTeam")


class _StubContainerNamespace:
    def list(self, *a, **kw):
        return []

    def run(self, *a, **kw):
        return None


class _StubDockerClient:
    def __init__(self):
        self.containers = _StubContainerNamespace()


def _stub_docker_from_env():
    return _StubDockerClient()


_stub_docker_module = types.ModuleType("docker")
_stub_docker_module.from_env = _stub_docker_from_env
sys.modules.setdefault("docker", _stub_docker_module)


def _stub_requests_call(*a, **kw):
    raise RuntimeError(
        "regex tests must not make HTTP calls — stub _fetch_github_linked_issues"
    )


_stub_requests_module = types.ModuleType("requests")
_stub_requests_module.get = _stub_requests_call
_stub_requests_module.post = _stub_requests_call
_stub_requests_module.delete = _stub_requests_call
sys.modules.setdefault("requests", _stub_requests_module)

# Make `agent-team/daemon/` importable when run directly.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import daemon  # noqa: E402  (must come after env/stub setup above)


REPO = os.environ["GITHUB_REPO"]
OTHER_REPO = "someone-else/different"


# ── _parse_dep_refs ───────────────────────────────────────────────────────


def test_dep_bare():
    assert daemon._parse_dep_refs("Depends on #5") == {5}
    assert daemon._parse_dep_refs("Blocked by #12 right now") == {12}
    assert daemon._parse_dep_refs("after #7") == {7}


def test_dep_case_insensitive():
    assert daemon._parse_dep_refs("DEPENDS ON #3") == {3}
    assert daemon._parse_dep_refs("bLoCkEd By #4") == {4}


def test_dep_url_same_repo():
    body = f"Depends on https://github.com/{REPO}/issues/42"
    assert daemon._parse_dep_refs(body) == {42}


def test_dep_url_cross_repo_ignored():
    body = f"Depends on https://github.com/{OTHER_REPO}/issues/42"
    assert daemon._parse_dep_refs(body) == set()


def test_dep_owner_repo_same_repo():
    body = f"Blocked by {REPO}#99"
    assert daemon._parse_dep_refs(body) == {99}


def test_dep_owner_repo_cross_repo_ignored():
    body = f"Blocked by {OTHER_REPO}#99"
    assert daemon._parse_dep_refs(body) == set()


def test_dep_multiple_refs_merged():
    body = (
        f"Depends on #1\n"
        f"Blocked by https://github.com/{REPO}/issues/2\n"
        f"After {REPO}#3\n"
    )
    assert daemon._parse_dep_refs(body) == {1, 2, 3}


def test_dep_empty_body():
    assert daemon._parse_dep_refs("") == set()
    assert daemon._parse_dep_refs(None) == set()


def test_dep_no_keyword_no_match():
    # Numeric mention without a keyword does NOT count as a dep.
    assert daemon._parse_dep_refs("See #42 for context") == set()


# ── _parse_pr_close_refs_from_body ────────────────────────────────────────


def test_pr_close_bare_existing_keywords():
    # The original keyword set (Closes / Fixes / Resolves / Part of)
    assert daemon._parse_pr_close_refs_from_body("Closes #5") == {5}
    assert daemon._parse_pr_close_refs_from_body("Fixes #6") == {6}
    assert daemon._parse_pr_close_refs_from_body("Resolves #7") == {7}
    assert daemon._parse_pr_close_refs_from_body("Part of #8") == {8}


def test_pr_close_bare_broadened_keywords():
    # New keywords added in #45
    assert daemon._parse_pr_close_refs_from_body("Implements #10") == {10}
    assert daemon._parse_pr_close_refs_from_body("Completes #11") == {11}
    assert daemon._parse_pr_close_refs_from_body("Closed-by #12") == {12}
    assert daemon._parse_pr_close_refs_from_body("Fix #13") == {13}
    assert daemon._parse_pr_close_refs_from_body("Close #14") == {14}
    assert daemon._parse_pr_close_refs_from_body("Resolve #15") == {15}
    assert daemon._parse_pr_close_refs_from_body("Fixed #16") == {16}
    assert daemon._parse_pr_close_refs_from_body("Closed #17") == {17}
    assert daemon._parse_pr_close_refs_from_body("Resolved #18") == {18}


def test_pr_close_case_insensitive():
    assert daemon._parse_pr_close_refs_from_body("CLOSES #1") == {1}
    assert daemon._parse_pr_close_refs_from_body("iMpLeMeNtS #2") == {2}


def test_pr_close_url_same_repo():
    body = f"Closes https://github.com/{REPO}/issues/123"
    assert daemon._parse_pr_close_refs_from_body(body) == {123}


def test_pr_close_url_cross_repo_ignored():
    body = f"Closes https://github.com/{OTHER_REPO}/issues/123"
    assert daemon._parse_pr_close_refs_from_body(body) == set()


def test_pr_close_owner_repo_same_repo():
    body = f"Fixes {REPO}#321"
    assert daemon._parse_pr_close_refs_from_body(body) == {321}


def test_pr_close_owner_repo_cross_repo_ignored():
    body = f"Fixes {OTHER_REPO}#321"
    assert daemon._parse_pr_close_refs_from_body(body) == set()


def test_pr_close_multiple_refs_merged():
    body = (
        f"Closes #1\n"
        f"Implements https://github.com/{REPO}/issues/2\n"
        f"Part of {REPO}#3\n"
        f"Completes #4\n"
    )
    assert daemon._parse_pr_close_refs_from_body(body) == {1, 2, 3, 4}


def test_pr_close_rejects_non_numeric():
    assert daemon._parse_pr_close_refs_from_body("Closes #abc") == set()
    assert daemon._parse_pr_close_refs_from_body("Fixes #") == set()


def test_pr_close_empty_body():
    assert daemon._parse_pr_close_refs_from_body("") == set()
    assert daemon._parse_pr_close_refs_from_body(None) == set()


def test_pr_close_no_keyword_no_match():
    # "See #5" should not count — needs an explicit close keyword.
    assert daemon._parse_pr_close_refs_from_body("See #5 for context") == set()


# ── _parse_issue_num_from_branch ──────────────────────────────────────────


def test_branch_frontend():
    assert daemon._parse_issue_num_from_branch("frontend/5-foo") == 5


def test_branch_backend():
    assert daemon._parse_issue_num_from_branch("backend/12-bar") == 12


def test_branch_fullstack():
    assert daemon._parse_issue_num_from_branch("fullstack/7-baz") == 7


def test_branch_multi_digit():
    assert daemon._parse_issue_num_from_branch("backend/123-some-long-slug") == 123


def test_branch_non_matching_prefix():
    assert daemon._parse_issue_num_from_branch("feat/5-foo") is None
    assert daemon._parse_issue_num_from_branch("docs/intro") is None
    assert daemon._parse_issue_num_from_branch("main") is None


def test_branch_no_number():
    # Branch with the right prefix but no leading number is not parseable.
    assert daemon._parse_issue_num_from_branch("backend/foo") is None


def test_branch_empty_or_none():
    assert daemon._parse_issue_num_from_branch("") is None
    assert daemon._parse_issue_num_from_branch(None) is None


# ── _parse_pr_close_refs (composite) ──────────────────────────────────────


def test_composite_body_wins_over_branch():
    pr = {
        "number": 100,
        "body": "Closes #42",
        "head": {"ref": "backend/99-foo"},
    }
    # Body has a direct ref — the branch should not be consulted.
    assert daemon._parse_pr_close_refs(pr) == {42}


def test_composite_branch_fallback_when_body_empty():
    pr = {
        "number": 100,
        "body": "(no description)",
        "head": {"ref": "frontend/77-some-slug"},
    }
    assert daemon._parse_pr_close_refs(pr) == {77}


def test_composite_returns_empty_when_nothing_matches():
    # Without overriding the GraphQL fetcher this test would attempt a real
    # network call; stub it to return empty so we exercise the fallback path
    # without touching the network.
    original = daemon._fetch_github_linked_issues
    daemon._fetch_github_linked_issues = lambda _pr_number: set()
    try:
        pr = {
            "number": 100,
            "body": "no link",
            "head": {"ref": "feat/foo"},
        }
        assert daemon._parse_pr_close_refs(pr) == set()
    finally:
        daemon._fetch_github_linked_issues = original


def test_composite_graphql_fallback_invoked_last():
    # Stub the GraphQL fetcher so we can verify it's the third fallback.
    calls = []

    def fake_fetch(pr_number):
        calls.append(pr_number)
        return {888}

    original = daemon._fetch_github_linked_issues
    daemon._fetch_github_linked_issues = fake_fetch
    try:
        pr = {
            "number": 200,
            "body": "Just some notes, no link",
            "head": {"ref": "feat/no-prefix"},
        }
        assert daemon._parse_pr_close_refs(pr) == {888}
        assert calls == [200]
    finally:
        daemon._fetch_github_linked_issues = original


# ── Regression: cross-repo refs do NOT contaminate same-repo results ──────


def test_mixed_same_and_cross_repo_only_same_kept():
    body = (
        f"Closes #1\n"
        f"Closes https://github.com/{OTHER_REPO}/issues/2\n"
        f"Closes {OTHER_REPO}#3\n"
    )
    assert daemon._parse_pr_close_refs_from_body(body) == {1}


# ── Runner ────────────────────────────────────────────────────────────────


def _run_all():
    failures = []
    fns = [
        v for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failures.append((fn.__name__, repr(e) or "assertion failed"))
        except Exception as e:
            failures.append((fn.__name__, f"{type(e).__name__}: {e}"))

    if failures:
        print(f"FAIL: {len(failures)} / {len(fns)} tests failed:")
        for name, msg in failures:
            print(f"  - {name}: {msg}")
        sys.exit(1)
    print(f"OK: {len(fns)} tests passed")


if __name__ == "__main__":
    _run_all()
