"""Tests for HPC connector security fixes."""
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


# Test _validate_job_id
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
