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


def run_command_streaming(
    args: List[str],
    cwd: Optional[Path] = None,
    stdout_path: Optional[Path] = None,
    stderr_path: Optional[Path] = None,
    on_stdout_line: Optional[callable] = None,
    on_stderr_line: Optional[callable] = None,
    timeout: Optional[int] = None,
) -> Tuple[int, dict]:
    """
    Run an external command with streaming output.
    
    This is designed for real-time log streaming during job execution.
    
    Behavior:
    - If stdout_path provided: stdout goes directly to file (e.g., MAFFT alignment)
    - Otherwise: stdout streams via on_stdout_line callback
    - If stderr_path provided: stderr goes to file AND on_stderr_line callback
    - If no stderr_path: stderr only via callback
    
    Args:
        args: Command and arguments as list
        cwd: Working directory
        stdout_path: File to write stdout to (for MAFFT output)
        stderr_path: File to write stderr to (for logs)
        on_stdout_line: Callback for each stdout line (if not stdout_path)
        on_stderr_line: Callback for each stderr line
        timeout: Optional timeout in seconds
    
    Returns:
        (exit_code, stats_dict)
        
        stats_dict contains:
        - stdout_lines: int (count)
        - stderr_lines: int (count)
        - stderr_tail: List[str] (last 30 lines for error reporting)
        - duration_seconds: float
    """
    import os
    import time
    from collections import deque
    
    start_time = time.time()
    
    stats = {
        "stdout_lines": 0,
        "stderr_lines": 0,
        "stderr_tail": [],
        "duration_seconds": 0.0,
    }
    
    # Ring buffer for last 30 stderr lines
    stderr_tail_buffer: deque = deque(maxlen=30)
    
    try:
        # Augment PATH
        env = os.environ.copy()
        current_path = env.get("PATH", "")
        if "/usr/bin" not in current_path:
            env["PATH"] = f"{current_path}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        
        logger.info(f"Running command (streaming): {' '.join(args)}")
        
        # Ensure cwd exists
        if cwd and not cwd.exists():
            stats["stderr_tail"] = [f"Working directory does not exist: {cwd}"]
            return -1, stats
        
        # Open stdout file if specified
        stdout_file = None
        if stdout_path:
            stdout_file = open(stdout_path, 'w', encoding='utf-8', newline='\n')
        
        # Open stderr file if specified
        stderr_file = None
        if stderr_path:
            stderr_path.parent.mkdir(parents=True, exist_ok=True)
            stderr_file = open(stderr_path, 'a')
            stderr_file.write(f"CMD: {' '.join(args)}\n")
            stderr_file.write("-" * 40 + "\n")
        
        process = None
        try:
            # Start process
            process = subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE if not stdout_file else stdout_file,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )
            
            # If stdout goes to file, we only need to handle stderr
            if stdout_file:
                # Stream stderr only
                for line in process.stderr:
                    stats["stderr_lines"] += 1
                    stderr_tail_buffer.append(line.rstrip())
                    
                    if stderr_file:
                        stderr_file.write(line)
                        stderr_file.flush()
                    
                    if on_stderr_line:
                        try:
                            on_stderr_line(line.rstrip())
                        except Exception:
                            pass  # Don't fail job due to callback error
            else:
                # Stream both stdout and stderr using select for non-blocking reads
                # This is more complex but handles interleaved output properly
                import selectors
                
                sel = selectors.DefaultSelector()
                if process.stdout:
                    sel.register(process.stdout, selectors.EVENT_READ, 'stdout')
                if process.stderr:
                    sel.register(process.stderr, selectors.EVENT_READ, 'stderr')
                
                while sel.get_map():
                    ready = sel.select(timeout=0.1)
                    for key, _ in ready:
                        stream = key.fileobj
                        stream_name = key.data
                        
                        line = stream.readline()
                        if not line:
                            sel.unregister(stream)
                            continue
                        
                        line = line.rstrip()
                        
                        if stream_name == 'stdout':
                            stats["stdout_lines"] += 1
                            if on_stdout_line:
                                try:
                                    on_stdout_line(line)
                                except Exception:
                                    pass
                        else:  # stderr
                            stats["stderr_lines"] += 1
                            stderr_tail_buffer.append(line)
                            
                            if stderr_file:
                                stderr_file.write(line + "\n")
                                stderr_file.flush()
                            
                            if on_stderr_line:
                                try:
                                    on_stderr_line(line)
                                except Exception:
                                    pass
                
                sel.close()
            
            # Wait for process to complete
            process.wait(timeout=timeout)
            exit_code = process.returncode
            
        finally:
            if stdout_file:
                stdout_file.close()
            if stderr_file:
                stderr_file.write("-" * 40 + "\n")
                stderr_file.write(f"Exit code: {process.returncode if 'process' in dir() else 'N/A'}\n")
                stderr_file.close()
        
        stats["duration_seconds"] = time.time() - start_time
        stats["stderr_tail"] = list(stderr_tail_buffer)
        
        return exit_code, stats
        
    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout}s: {args}")
        if process:
            process.kill()
        stats["duration_seconds"] = time.time() - start_time
        stats["stderr_tail"] = list(stderr_tail_buffer)
        stats["stderr_tail"].append(f"[TIMEOUT] Command killed after {timeout}s")
        return -1, stats
        
    except Exception as e:
        logger.exception(f"Exception running command: {args}")
        stats["duration_seconds"] = time.time() - start_time
        stats["stderr_tail"] = list(stderr_tail_buffer)
        stats["stderr_tail"].append(f"[ERROR] {str(e)}")
        return -1, stats
