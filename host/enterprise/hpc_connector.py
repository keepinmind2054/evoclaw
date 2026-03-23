"""
HPC Connector — Phase 3 Enterprise Suite

Submits and monitors jobs on SLURM/PBS HPC clusters.
"""
import os
import re
import logging
import shlex
import subprocess
import tempfile
from typing import Optional, Dict, List
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# Only allow job IDs that consist of word characters, dots, and hyphens.
# This prevents shell injection when job_id is interpolated into SSH commands.
_SAFE_JOB_ID_RE = re.compile(r'^[\w.\-]+$')

# Maximum bytes we will read back from a job output file over SSH.
# Prevents a runaway HPC job from OOM-ing the bot process.
_JOB_OUTPUT_MAX_BYTES = 256 * 1024  # 256 KB

# Allowlist of characters permitted in job names.
# Anything outside this set is stripped before being embedded in scheduler
# directives, preventing newline-injection into the #SBATCH / #PBS header.
_SAFE_NAME_RE = re.compile(r'[^A-Za-z0-9_.\-]')


def _sanitize_job_name(name: str) -> str:
    """Strip characters that are unsafe inside SLURM/PBS header directives.

    SLURM job names are embedded verbatim in the #SBATCH lines written to a
    shell script, so a name containing a newline would allow an attacker to
    inject arbitrary scheduler directives (e.g. ``--wrap=malicious_cmd``).
    We replace every unsafe character with ``_`` and cap the length at 63
    characters (SLURM's documented maximum).
    """
    sanitized = _SAFE_NAME_RE.sub("_", name)
    return sanitized[:63]


def _validate_job_id(job_id: str) -> str:
    """Raise ValueError if job_id contains shell-unsafe characters."""
    if not _SAFE_JOB_ID_RE.match(job_id):
        raise ValueError(f"Invalid job_id: {job_id!r}")
    return job_id


class JobStatus(str, Enum):
    PENDING   = "PENDING"
    RUNNING   = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED    = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN   = "UNKNOWN"


@dataclass
class HPCJob:
    job_id: str
    name: str
    status: JobStatus
    nodes: int = 1
    cpus: int = 1
    memory_gb: int = 4
    runtime_hours: float = 1.0
    script: str = ""
    output_file: Optional[str] = None


class HPCConnector:
    """
    HPC cluster connector (SLURM/PBS).

    Config via environment:
      HPC_SCHEDULER   — "slurm" | "pbs"
      HPC_HOST        — cluster head node (for SSH submission)
      HPC_SSH_KEY     — SSH key path
      HPC_PARTITION   — default partition/queue
    """

    def __init__(
        self,
        scheduler: Optional[str] = None,
        host: Optional[str] = None,
        partition: Optional[str] = None,
    ):
        self.scheduler = (scheduler or os.getenv("HPC_SCHEDULER", "slurm")).lower()
        self.host = host or os.getenv("HPC_HOST", "")
        self.partition = partition or os.getenv("HPC_PARTITION", "default")
        self.ssh_key = os.getenv("HPC_SSH_KEY", "")

    def _run_remote(self, cmd: str) -> str:
        """Run a command on the HPC head node via SSH.

        FIX (P14D-HPC-1): The previous implementation passed the remote
        command as a single string element in the argv list, meaning SSH
        executed it verbatim on the remote shell.  That is intentional and
        correct for SSH — the *remote* shell parses the command string, not
        the local subprocess machinery.  However, we must ensure callers
        never build ``cmd`` from un-validated user input.  All call-sites in
        this class use ``shlex.quote()`` on every variable before embedding
        it in ``cmd``, so remote shell injection is prevented at the call-site
        level.

        FIX (P14D-HPC-2): Added ``stdin=subprocess.DEVNULL`` so the SSH
        process never blocks waiting for interactive input from the bot's
        stdin (e.g. host-key confirmation prompts).

        FIX (P14D-HPC-3): Return code is always checked; non-zero exits are
        surfaced to the caller via RuntimeError so failures are never silently
        swallowed.
        """
        if not self.host:
            raise RuntimeError("HPC_HOST not configured")
        ssh_args = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
        if self.ssh_key:
            ssh_args += ["-i", self.ssh_key]
        ssh_args += [self.host, cmd]
        try:
            result = subprocess.run(
                ssh_args,
                capture_output=True,
                text=True,
                timeout=30,
                stdin=subprocess.DEVNULL,  # P14D-HPC-2: prevent stdin blocking
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("SSH command timed out after 30s")
        if result.returncode != 0:
            raise RuntimeError(
                f"SSH command failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def submit_job(
        self,
        name: str,
        script: str,
        nodes: int = 1,
        cpus: int = 1,
        memory_gb: int = 4,
        runtime_hours: float = 1.0,
        partition: Optional[str] = None,
    ) -> Optional["HPCJob"]:
        """Submit a job to the HPC scheduler."""
        # P14D-HPC-4: sanitize job name before embedding it in scheduler headers
        safe_name = _sanitize_job_name(name)
        part = partition or self.partition
        if self.scheduler == "slurm":
            return self._submit_slurm(safe_name, script, nodes, cpus, memory_gb, runtime_hours, part)
        elif self.scheduler == "pbs":
            return self._submit_pbs(safe_name, script, nodes, cpus, memory_gb, runtime_hours, part)
        else:
            logger.error(f"Unknown scheduler: {self.scheduler}")
            return None

    def _submit_slurm(self, name, script, nodes, cpus, memory_gb, runtime_hours, partition) -> Optional[HPCJob]:
        hours = int(runtime_hours)
        mins = int((runtime_hours - hours) * 60)
        # P14D-HPC-4: name is already sanitized by submit_job(); embed it without
        # additional quoting in the header directive (the value is safe).  The
        # --wrap flag receives the full script body quoted with shlex.quote().
        sbatch = f"""#!/bin/bash
#SBATCH --job-name={name}
#SBATCH --nodes={nodes}
#SBATCH --ntasks-per-node={cpus}
#SBATCH --mem={memory_gb}G
#SBATCH --time={hours:02d}:{mins:02d}:00
#SBATCH --partition={partition}
#SBATCH --output={name}_%j.out

{script}
"""
        try:
            out = self._run_remote(
                f"sbatch --job-name={shlex.quote(name)} --nodes={shlex.quote(str(nodes))}"
                f" --ntasks-per-node={shlex.quote(str(cpus))}"
                f" --mem={shlex.quote(str(memory_gb) + 'G')} --time={hours:02d}:{mins:02d}:00"
                f" --partition={shlex.quote(partition)} --output={shlex.quote(name + '_%j.out')}"
                f" --wrap={shlex.quote(sbatch)}"
            )
            # "Submitted batch job 12345" — take the last token
            tokens = out.split()
            if not tokens:
                raise RuntimeError("sbatch returned empty output")
            job_id = tokens[-1]
            _validate_job_id(job_id)  # sanity-check the returned id
            return HPCJob(job_id=job_id, name=name, status=JobStatus.PENDING,
                         nodes=nodes, cpus=cpus, memory_gb=memory_gb,
                         runtime_hours=runtime_hours, script=script)
        except Exception as e:
            logger.error(f"SLURM submit failed: {e}")
            return None

    def _submit_pbs(self, name, script, nodes, cpus, memory_gb, runtime_hours, partition) -> Optional[HPCJob]:
        hours = int(runtime_hours)
        mins = int((runtime_hours - hours) * 60)
        # P14D-HPC-4: name is already sanitized; safe to embed in PBS directives.
        pbs_script = f"""#!/bin/bash
#PBS -N {name}
#PBS -l nodes={nodes}:ppn={cpus}
#PBS -l mem={memory_gb}gb
#PBS -l walltime={hours:02d}:{mins:02d}:00
#PBS -q {partition}

{script}
"""
        try:
            fname = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.pbs', delete=False
                ) as f:
                    f.write(pbs_script)
                    fname = f.name
                scp_args = ["scp", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes"]
                if self.ssh_key:
                    scp_args += ["-i", self.ssh_key]
                remote_path = f"/tmp/{os.path.basename(fname)}"
                scp_args += [fname, f"{self.host}:{remote_path}"]
                # P14D-HPC-2: close stdin on scp as well
                subprocess.run(scp_args, check=True, timeout=30, stdin=subprocess.DEVNULL)
                out = self._run_remote(f"qsub {shlex.quote(remote_path)}; rm -f {shlex.quote(remote_path)}")
            finally:
                if fname and os.path.exists(fname):
                    os.unlink(fname)
            job_id = out.strip()
            _validate_job_id(job_id)  # sanity-check the returned id
            return HPCJob(job_id=job_id, name=name, status=JobStatus.PENDING,
                         nodes=nodes, cpus=cpus, memory_gb=memory_gb,
                         runtime_hours=runtime_hours, script=script)
        except Exception as e:
            logger.error(f"PBS submit failed: {e}")
            return None

    def get_job_status(self, job_id: str) -> JobStatus:
        """Query job status."""
        try:
            _validate_job_id(job_id)
            safe_id = shlex.quote(job_id)
            if self.scheduler == "slurm":
                out = self._run_remote(f"squeue -j {safe_id} -h -o %T 2>/dev/null || sacct -j {safe_id} -n -o State%20")
            else:
                out = self._run_remote(f"qstat -f {safe_id} | grep job_state")
            out = out.strip().upper()
            for status in JobStatus:
                if status.value in out:
                    return status
        except Exception:
            pass
        return JobStatus.UNKNOWN

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a running or pending job."""
        try:
            _validate_job_id(job_id)
            safe_id = shlex.quote(job_id)
            cmd = f"scancel {safe_id}" if self.scheduler == "slurm" else f"qdel {safe_id}"
            self._run_remote(cmd)
            return True
        except Exception as e:
            logger.error(f"Cancel failed for {job_id}: {e}")
            return False

    def fetch_job_output(self, job_id: str, output_file: str) -> str:
        """Fetch the tail of a job's output file from the HPC head node.

        P14D-HPC-5: Size-limit the fetch so a runaway job producing gigabytes
        of output cannot OOM the bot.  We retrieve at most _JOB_OUTPUT_MAX_BYTES
        bytes using ``tail -c`` on the remote side.
        """
        _validate_job_id(job_id)
        safe_file = shlex.quote(output_file)
        limit = _JOB_OUTPUT_MAX_BYTES
        try:
            return self._run_remote(f"tail -c {limit} {safe_file} 2>/dev/null || echo '(output file not found)'")
        except Exception as e:
            logger.error(f"fetch_job_output failed for {job_id}: {e}")
            return f"(error fetching output: {e})"

    def is_configured(self) -> bool:
        return bool(self.host)
