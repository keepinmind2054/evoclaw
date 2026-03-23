"""Tests for HPC connector security fixes (P14D)."""
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ── _validate_job_id ──────────────────────────────────────────────────────────

def test_valid_job_id_alphanumeric():
    from host.enterprise.hpc_connector import _validate_job_id
    assert _validate_job_id("12345") == "12345"
    assert _validate_job_id("job.123") == "job.123"
    assert _validate_job_id("job-123_abc") == "job-123_abc"


def test_invalid_job_id_semicolon():
    from host.enterprise.hpc_connector import _validate_job_id
    with pytest.raises(ValueError):
        _validate_job_id("123; rm -rf /")


def test_invalid_job_id_backtick():
    from host.enterprise.hpc_connector import _validate_job_id
    with pytest.raises(ValueError):
        _validate_job_id("123`whoami`")


def test_invalid_job_id_pipe():
    from host.enterprise.hpc_connector import _validate_job_id
    with pytest.raises(ValueError):
        _validate_job_id("123 | cat /etc/passwd")


# ── _sanitize_job_name (P14D-HPC-4) ──────────────────────────────────────────

def test_sanitize_job_name_safe():
    from host.enterprise.hpc_connector import _sanitize_job_name
    assert _sanitize_job_name("my-job_1.0") == "my-job_1.0"


def test_sanitize_job_name_strips_newline():
    """Newlines in job names would inject extra #SBATCH directives."""
    from host.enterprise.hpc_connector import _sanitize_job_name
    result = _sanitize_job_name("legit\n#SBATCH --wrap=malicious")
    assert "\n" not in result
    assert "#" not in result


def test_sanitize_job_name_strips_semicolon():
    from host.enterprise.hpc_connector import _sanitize_job_name
    result = _sanitize_job_name("job;rm -rf /")
    assert ";" not in result
    assert " " not in result


def test_sanitize_job_name_max_length():
    from host.enterprise.hpc_connector import _sanitize_job_name
    long_name = "a" * 200
    result = _sanitize_job_name(long_name)
    assert len(result) <= 63


def test_sanitize_job_name_special_chars():
    from host.enterprise.hpc_connector import _sanitize_job_name
    result = _sanitize_job_name("job$(whoami)")
    assert "$" not in result
    assert "(" not in result
    assert ")" not in result


# ── JIRA key validation (P14D-JIRA-3) ────────────────────────────────────────

def test_jira_valid_issue_key():
    from host.enterprise.jira_connector import _validate_issue_key
    assert _validate_issue_key("PROJ-123") == "PROJ-123"
    assert _validate_issue_key("ABC-1") == "ABC-1"


def test_jira_invalid_issue_key_path_traversal():
    from host.enterprise.jira_connector import _validate_issue_key
    with pytest.raises(ValueError):
        _validate_issue_key("../admin")


def test_jira_invalid_issue_key_injection():
    from host.enterprise.jira_connector import _validate_issue_key
    with pytest.raises(ValueError):
        _validate_issue_key("PROJ-123/comment/../../../config")


def test_jira_valid_project_key():
    from host.enterprise.jira_connector import _validate_project_key
    assert _validate_project_key("MYPROJ") == "MYPROJ"


def test_jira_invalid_project_key_lowercase():
    from host.enterprise.jira_connector import _validate_project_key
    with pytest.raises(ValueError):
        _validate_project_key("myproj")


def test_jira_search_issues_max_results_cap():
    """search_issues() should cap max_results to _MAX_SEARCH_RESULTS."""
    from host.enterprise.jira_connector import JiraConnector, _MAX_SEARCH_RESULTS
    import unittest.mock as mock

    connector = JiraConnector.__new__(JiraConnector)
    connector._session = None  # No real HTTP session
    connector.base_url = "https://example.atlassian.net"

    captured = {}

    def fake_get(path, params=None):
        captured["params"] = params
        return {"issues": []}

    connector._get = fake_get

    connector.search_issues("project = TEST", max_results=10 ** 9)
    assert captured["params"]["maxResults"] == _MAX_SEARCH_RESULTS


# ── LDAP injection prevention (P14D-LDAP-4) ──────────────────────────────────

def test_ldap_escape_used_in_lookup():
    """lookup_user must escape the username before building the LDAP filter."""
    pytest.importorskip("ldap3")
    from host.enterprise.ldap_connector import LDAPConnector
    import unittest.mock as mock

    connector = LDAPConnector.__new__(LDAPConnector)
    connector.base_dn = "DC=example,DC=com"
    connector._conn = mock.MagicMock()
    connector._conn.closed = False
    connector._conn.entries = []

    connector.lookup_user("*)(uid=*)(|(uid=*")
    # The search filter passed to ldap3 must not contain the raw injection string
    call_args = connector._conn.search.call_args
    ldap_filter = call_args[0][1]  # second positional arg to search()
    assert "*)(uid=*)(|(uid=*" not in ldap_filter


# ── Workflow cycle detection (P14D-WF-2) ─────────────────────────────────────

def test_workflow_cycle_detected():
    from host.enterprise.workflow_engine import WorkflowDAG

    dag = WorkflowDAG("cycle-test")

    @dag.step("a", depends_on=["b"])
    async def step_a(ctx):
        return {}

    @dag.step("b", depends_on=["a"])
    async def step_b(ctx):
        return {}

    with pytest.raises(ValueError, match="[Cc]ycle"):
        dag._validate()


def test_workflow_undefined_dep_detected():
    """P14D-WF-1: a step depending on a non-existent step should fail validation."""
    from host.enterprise.workflow_engine import WorkflowDAG

    dag = WorkflowDAG("undef-dep-test")

    @dag.step("a", depends_on=["nonexistent"])
    async def step_a(ctx):
        return {}

    with pytest.raises(ValueError, match="unknown step"):
        dag._validate()


def test_workflow_valid_dag_passes_validation():
    from host.enterprise.workflow_engine import WorkflowDAG

    dag = WorkflowDAG("valid-test")

    @dag.step("build")
    async def build(ctx):
        return {"artifact": "app.tar.gz"}

    @dag.step("test", depends_on=["build"])
    async def test(ctx):
        return {"passed": True}

    # Should not raise
    dag._validate()
