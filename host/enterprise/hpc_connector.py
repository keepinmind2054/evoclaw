"""
HPC Connector — Phase 3 Enterprise Suite

Submits and monitors jobs on SLURM/PBS HPC clusters.
"""
import os
import logging
import subprocess
import tempfile
from typing import Optional, Dict, List
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


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
        """Run a command on the HPC head node via SSH."""
        if not self.host:
            raise RuntimeError("HPC_HOST not configured")
        ssh_args = ["ssh", "-o", "StrictHostKeyChecking=no"]
        if self.ssh_key:
            ssh_args += ["-i", self.ssh_key]
        ssh_args += [self.host, cmd]
        result = subprocess.run(ssh_args, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"SSH command failed: {result.stderr}")
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
    ) -> Optional[HPCJob]:
        """Submit a job to the HPC scheduler."""
        part = partition or self.partition
        if self.scheduler == "slurm":
            return self._submit_slurm(name, script, nodes, cpus, memory_gb, runtime_hours, part)
        elif self.scheduler == "pbs":
            return self._submit_pbs(name, script, nodes, cpus, memory_gb, runtime_hours, part)
        else:
            logger.error(f"Unknown scheduler: {self.scheduler}")
            return None

    def _submit_slurm(self, name, script, nodes, cpus, memory_gb, runtime_hours, partition) -> Optional[HPCJob]:
        hours = int(runtime_hours)
        mins = int((runtime_hours - hours) * 60)
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
            # Use sbatch --wrap to avoid writing a temp file to the local filesystem
            # that the remote head node cannot access.  The script content is passed
            # inline via the heredoc-style --wrap flag so no file transfer is needed.
            out = self._run_remote(
                f"sbatch --job-name={name} --nodes={nodes} --ntasks-per-node={cpus}"
                f" --mem={memory_gb}G --time={hours:02d}:{mins:02d}:00"
                f" --partition={partition} --output={name}_%j.out"
                f" --wrap={sbatch!r}"
            )
            job_id = out.split()[-1]  # "Submitted batch job 12345"
            return HPCJob(job_id=job_id, name=name, status=JobStatus.PENDING,
                         nodes=nodes, cpus=cpus, memory_gb=memory_gb,
                         runtime_hours=runtime_hours, script=script)
        except Exception as e:
            logger.error(f"SLURM submit failed: {e}")
            return None

    def _submit_pbs(self, name, script, nodes, cpus, memory_gb, runtime_hours, partition) -> Optional[HPCJob]:
        hours = int(runtime_hours)
        mins = int((runtime_hours - hours) * 60)
        pbs_script = f"""#!/bin/bash
#PBS -N {name}
#PBS -l nodes={nodes}:ppn={cpus}
#PBS -l mem={memory_gb}gb
#PBS -l walltime={hours:02d}:{mins:02d}:00
#PBS -q {partition}

{script}
"""
        try:
            # Write script to a local temp file, copy it to the remote host via SSH,
            # submit it there, then clean up the local temp file.
            fname = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.pbs', delete=False
                ) as f:
                    f.write(pbs_script)
                    fname = f.name
                # scp the script to the remote head node, then submit
                scp_args = ["scp", "-o", "StrictHostKeyChecking=no"]
                if self.ssh_key:
                    scp_args += ["-i", self.ssh_key]
                remote_path = f"/tmp/{os.path.basename(fname)}"
                scp_args += [fname, f"{self.host}:{remote_path}"]
                subprocess.run(scp_args, check=True, timeout=30)
                out = self._run_remote(f"qsub {remote_path}; rm -f {remote_path}")
            finally:
                if fname and os.path.exists(fname):
                    os.unlink(fname)
            job_id = out.strip()
            return HPCJob(job_id=job_id, name=name, status=JobStatus.PENDING,
                         nodes=nodes, cpus=cpus, memory_gb=memory_gb,
                         runtime_hours=runtime_hours, script=script)
        except Exception as e:
            logger.error(f"PBS submit failed: {e}")
            return None

    def get_job_status(self, job_id: str) -> JobStatus:
        """Query job status."""
        try:
            if self.scheduler == "slurm":
                out = self._run_remote(f"squeue -j {job_id} -h -o %T 2>/dev/null || sacct -j {job_id} -n -o State%20")
            else:
                out = self._run_remote(f"qstat -f {job_id} | grep job_state")
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
            cmd = f"scancel {job_id}" if self.scheduler == "slurm" else f"qdel {job_id}"
            self._run_remote(cmd)
            return True
        except Exception as e:
            logger.error(f"Cancel failed for {job_id}: {e}")
            return False

    def is_configured(self) -> bool:
        return bool(self.host)
