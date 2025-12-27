import subprocess
import logging
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

def run_command(args: List[str], cwd: Optional[Path] = None, log_file: Optional[Path] = None) -> Tuple[int, str, str]:
    """
    Run an external command safely.

    - args: list of command and arguments (no shell=True).
    - cwd: optional working directory.
    - log_file: optional path to append stdout/stderr.

    Returns (returncode, stdout, stderr).
    Raises no exceptions; caller decides what to do.
    """
    try:
        import os
        # Augment PATH if it's too restricted (e.g. in some systemd environments)
        env = os.environ.copy()
        current_path = env.get("PATH", "")
        if "/usr/bin" not in current_path:
            env["PATH"] = f"{current_path}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            
        logger.info(f"Running command: {' '.join(args)}")
        
        # Ensure cwd exists if provided
        if cwd and not cwd.exists():
            return -1, "", f"Working directory does not exist: {cwd}"

        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False
        )
        
        stdout = result.stdout
        stderr = result.stderr
        
        if log_file:
            try:
                with open(log_file, 'a') as f:
                    f.write(f"CMD: {' '.join(args)}\n")
                    f.write(f"STDOUT:\n{stdout}\n")
                    f.write(f"STDERR:\n{stderr}\n")
                    f.write("-" * 40 + "\n")
            except Exception as e:
                logger.error(f"Failed to write to log file {log_file}: {e}")

        return result.returncode, stdout, stderr

    except Exception as e:
        logger.exception(f"Exception running command: {args}")
        return -1, "", str(e)
