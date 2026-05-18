"""Minimal vanilla-assert tests for the unblock + re-triage helpers.

These tests don't depend on `pytest`. Run with:

    GITHUB_TOKEN=x GITHUB_REPO=x/y python3 -m agent-team.daemon.tests.test_unblock

The daemon module imports `docker` and `requests` at top level and reads
`GITHUB_TOKEN` / `GITHUB_REPO` from the environment. We stub both modules
and set placeholder env vars so importing the module succeeds in CI / local
environments without those dependencies installed.
"""
import os
import sys
import types

# ── Stub third-party imports the daemon does at module load ─────────
sys.modules.setdefault("docker", types.SimpleNamespace(
    from_env=lambda: types.SimpleNamespace(
        containers=types.SimpleNamespace(list=lambda **_: [], run=lambda **_: None),
    ),
))


class _FakeRequests(types.ModuleType):
    """Minimal stand-in for the parts of `requests` daemon.py imports."""
    def __init__(self):
        super().__init__("requests")
        self.exceptions = types.SimpleNamespace(RequestException=Exception)

    def get(self, *args, **kwargs):
        raise RuntimeError("requests.get should be stubbed at the gh_get level for tests")

    def post(self, *args, **kwargs):
        return types.SimpleNamespace(status_code=200, json=lambda: {})

    def delete(self, *args, **kwargs):
        return types.SimpleNamespace(status_code=204)


sys.modules.setdefault("requests", _FakeRequests())

# ── Placeholder env vars (daemon reads these at import time) ────────
os.environ.setdefault("GITHUB_TOKEN", "test-token")
os.environ.setdefault("GITHUB_REPO", "test-owner/test-repo")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")

# Make the daemon package importable without polluting sys.path globally.
HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON_DIR = os.path.dirname(HERE)
if DAEMON_DIR not in sys.path:
    sys.path.insert(0, DAEMON_DIR)

import daemon as d  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────

class _GhGetStub:
    """Lets each test specify what gh_get returns for /issues/N lookups."""
    def __init__(self, dep_states):
        # dep_states: {issue_number: "open" | "closed"}
        self.dep_states = dep_states
        self.calls = []

    def __call__(self, path, params=None):
        self.calls.append(path)
        # Path looks like "/repos/{owner}/{repo}/issues/{N}"
        try:
            num = int(path.rstrip("/").split("/")[-1])
        except ValueError:
            return None
        state = self.dep_states.get(num)
        if state is None:
            raise RuntimeError(f"unexpected dep lookup for #{num}")
        return {"number": num, "state": state}


def _issue(number, body="", labels=()):
    return {
        "number": number,
        "body": body,
        "labels": [{"name": l} for l in labels],
    }


# ── Tests ───────────────────────────────────────────────────────────

def test_is_unblock_candidate_no_deps():
    ok, deps = d._is_unblock_candidate(_issue(10, body="no deps here"))
    assert ok is False
    assert deps == []


def test_is_unblock_candidate_all_closed(monkey):
    monkey.setattr(d, "gh_get", _GhGetStub({1: "closed", 2: "closed"}))
    ok, deps = d._is_unblock_candidate(
        _issue(10, body="Depends on #1 and blocked by #2"),
    )
    assert ok is True
    assert deps == [1, 2]


def test_is_unblock_candidate_one_open(monkey):
    monkey.setattr(d, "gh_get", _GhGetStub({1: "closed", 2: "open"}))
    ok, deps = d._is_unblock_candidate(
        _issue(10, body="Depends on #1\nAfter #2"),
    )
    assert ok is False
    assert deps == []


def test_is_unblock_candidate_after_keyword(monkey):
    monkey.setattr(d, "gh_get", _GhGetStub({5: "closed"}))
    ok, deps = d._is_unblock_candidate(_issue(11, body="after #5"))
    assert ok is True
    assert deps == [5]


def test_try_unblock_reentry_guard_skips_when_architect_in_progress(monkey):
    """If `blocked` was already stripped AND `architect-in-progress` is set,
    `_try_unblock_issue` must not re-dispatch — otherwise periodic sweeps
    would spam the architect with duplicate re-triage runs."""
    monkey.setattr(d, "gh_get", _GhGetStub({3: "closed"}))
    dispatched = []
    monkey.setattr(d, "dispatch_architect_retriage",
                   lambda issue, deps: dispatched.append((issue["number"], deps)))

    issue = _issue(20, body="Depends on #3",
                   labels=("architect-in-progress",))
    assert d._try_unblock_issue(issue) is False
    assert dispatched == []


def test_try_unblock_dispatches_when_blocked_label_present(monkey):
    monkey.setattr(d, "gh_get", _GhGetStub({3: "closed"}))
    dispatched = []
    monkey.setattr(d, "dispatch_architect_retriage",
                   lambda issue, deps: dispatched.append((issue["number"], deps)))
    monkey.setattr(d, "gh_remove_label", lambda *a, **k: None)
    monkey.setattr(d, "gh_comment", lambda *a, **k: None)

    issue = _issue(21, body="Depends on #3", labels=("blocked",))
    # `_try_unblock_issue` re-fetches the issue after stripping `blocked`;
    # make `gh_get(/repos/.../issues/21)` return the same issue without
    # blocked.
    original_get = d.gh_get

    def _routing_get(path, params=None):
        if path.endswith(f"/issues/{issue['number']}"):
            return _issue(21, body="Depends on #3", labels=())
        return original_get(path, params)

    monkey.setattr(d, "gh_get", _routing_get)

    assert d._try_unblock_issue(issue) is True
    assert dispatched == [(21, [3])]


def test_handle_agent_success_clears_retriage_key_for_architect(monkey):
    """A successful architect run must clear `architect-retriage-{N}` so a
    *next* unblock event for the same issue isn't silently dropped by the
    `state.already_handled` guard."""
    # Pre-seed handled set
    d.state.processed.add("architect-retriage-42")
    monkey.setattr(d, "gh_remove_label", lambda *a, **k: None)

    info = {
        "role": "architect",
        "number": 42,
        "name": "agent-team-architect-42-test",
        "action": "re_triage_unblocked",
        "container": types.SimpleNamespace(logs=lambda **_: b""),
    }
    d._handle_agent_success(info)
    assert "architect-retriage-42" not in d.state.processed


def test_handle_agent_success_strips_in_progress_label(monkey):
    """Defensive label-strip in `_handle_agent_success` — even when the
    prompt's bash step didn't run (e.g. container crashed mid-run)."""
    removed = []
    monkey.setattr(d, "gh_remove_label",
                   lambda n, l: removed.append((n, l)))

    info = {
        "role": "architect",
        "number": 50,
        "name": "agent-team-architect-50-test",
        "action": "plan_feature",
        "container": types.SimpleNamespace(logs=lambda **_: b""),
    }
    d._handle_agent_success(info)
    assert (50, "architect-in-progress") in removed


# ── Tiny monkeypatch shim so we don't need pytest ───────────────────

class _Monkey:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name, value=None):
        # Support both (obj, name, value) and (path, value) forms.
        if value is None and isinstance(name, object) and not isinstance(name, str):
            raise TypeError("must be (obj, name, value)")
        original = getattr(target, name)
        self._undo.append((target, name, original))
        setattr(target, name, value)

    def undo(self):
        for target, name, original in reversed(self._undo):
            setattr(target, name, original)
        self._undo.clear()


def _run_all():
    tests = [
        ("test_is_unblock_candidate_no_deps", test_is_unblock_candidate_no_deps, False),
        ("test_is_unblock_candidate_all_closed", test_is_unblock_candidate_all_closed, True),
        ("test_is_unblock_candidate_one_open", test_is_unblock_candidate_one_open, True),
        ("test_is_unblock_candidate_after_keyword", test_is_unblock_candidate_after_keyword, True),
        ("test_try_unblock_reentry_guard_skips_when_architect_in_progress",
         test_try_unblock_reentry_guard_skips_when_architect_in_progress, True),
        ("test_try_unblock_dispatches_when_blocked_label_present",
         test_try_unblock_dispatches_when_blocked_label_present, True),
        ("test_handle_agent_success_clears_retriage_key_for_architect",
         test_handle_agent_success_clears_retriage_key_for_architect, True),
        ("test_handle_agent_success_strips_in_progress_label",
         test_handle_agent_success_strips_in_progress_label, True),
    ]
    passed = 0
    failed = 0
    for name, fn, needs_monkey in tests:
        monkey = _Monkey()
        try:
            if needs_monkey:
                fn(monkey)
            else:
                fn()
            print(f"  ✓ {name}")
            passed += 1
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
            failed += 1
        finally:
            monkey.undo()
    print(f"\n{passed}/{passed+failed} passed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
