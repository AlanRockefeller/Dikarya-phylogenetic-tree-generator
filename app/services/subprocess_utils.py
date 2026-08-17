import subprocess
import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# RQ raises this in the worker thread when a job outlives its job_timeout. It
# subclasses Exception, so it must be caught ahead of any catch-all or it gets
# misreported as a tool crash. Kept as a tuple, and empty when RQ is absent, so
# this module stays importable outside the worker.
try:
    from rq.timeouts import JobTimeoutException as _RQJobTimeoutException
    _JOB_TIMEOUT_EXCEPTIONS = (_RQJobTimeoutException,)
except Exception:  # pragma: no cover - RQ not installed
    _JOB_TIMEOUT_EXCEPTIONS = ()


_LIMITER_WARNING_EMITTED = False

# Standard locations for util-linux prlimit. shutil.which() only searches PATH, and
# Gunicorn runs with a PATH that does not include /usr/bin -- so in production the web
# process silently ran MAFFT/trimal/FastTree (tree recomputation happens in-request)
# with no memory or CPU ceiling at all, while the RQ worker was correctly limited.
_LIMITER_FALLBACK_PATHS = ("/usr/bin/prlimit", "/bin/prlimit", "/usr/local/bin/prlimit")


def _find_limiter() -> Optional[str]:
    """Locate util-linux prlimit on PATH, then at its standard absolute paths."""
    limiter = shutil.which("prlimit")
    if limiter:
        return limiter
    for candidate in _LIMITER_FALLBACK_PATHS:
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _build_limited_argv(
    args: List[str], cpu_limit_seconds: Optional[int] = None
) -> Tuple[List[str], bool]:
    """Apply rlimits through util-linux ``prlimit``, never ``preexec_fn``.

    Python warns that ``preexec_fn`` can deadlock after fork in a threaded
    application.  If the safe argv wrapper is unavailable, run normally and
    log the missing protection rather than falling back to child-side Python.
    """
    from app.config import Config

    memory_mb = int(getattr(Config, "SUBPROCESS_MEMORY_LIMIT_MB", 0) or 0)
    cpu_seconds = (
        int(cpu_limit_seconds)
        if cpu_limit_seconds is not None
        else int(getattr(Config, "SUBPROCESS_CPU_LIMIT_SECONDS", 0) or 0)
    )
    limiter = _find_limiter()
    if not limiter:
        global _LIMITER_WARNING_EMITTED
        if not _LIMITER_WARNING_EMITTED:
            logger.warning(
                "DEGRADED subprocess_limits_unavailable: "
                "util-linux prlimit is unavailable; subprocess memory/CPU/core "
                "limits cannot be applied safely on this platform"
            )
            _LIMITER_WARNING_EMITTED = True
        return list(args), False

    limited = [limiter, "--core=0:0"]
    if memory_mb > 0:
        memory_bytes = memory_mb * 1024 * 1024
        limited.append(f"--as={memory_bytes}:{memory_bytes}")
    if cpu_seconds > 0:
        # A soft SIGXCPU remains distinguishable from the hard SIGKILL.
        limited.append(f"--cpu={cpu_seconds}:{cpu_seconds + 30}")
    limited.extend(["--", *args])
    return limited, True


def _describe_termination_signal(exit_code: int) -> Optional[str]:
    """Explain a negative exit code, so a resource kill is not just '-24'."""
    if exit_code >= 0:
        return None

    explanations = {
        24: "[LIMIT] Killed by SIGXCPU -- exceeded SUBPROCESS_CPU_LIMIT_SECONDS.",
        9: "[LIMIT] Killed by SIGKILL -- the kernel OOM killer, the RQ job timeout, or a worker restart.",
        11: "[CRASH] Killed by SIGSEGV -- the tool crashed. Keep the input; it is worth reporting upstream.",
        6: "[CRASH] Killed by SIGABRT -- the tool aborted, often a failed allocation under SUBPROCESS_MEMORY_LIMIT_MB.",
    }
    return explanations.get(-exit_code)


# Returned when the program could not be launched at all -- the configured
# binary does not exist, or is not executable. No real tool exits with this, so
# it is safe to use as a sentinel. Previously this case came back as a generic
# -1, which reached the job page as "failed with exit code -1" and told nobody
# anything.
EXIT_CODE_TOOL_NOT_FOUND = -127

# Returned when the tool was still running when its time budget ran out --
# either the subprocess `timeout` or RQ's job-level death penalty. Previously
# the RQ case fell through the catch-all below and was reported as a bare -1,
# which read as a tool crash rather than "this took too long".
EXIT_CODE_JOB_TIMEOUT = -128


def tool_failure_message(tool_label: str, exit_code: int,
                         time_limit_hours: Optional[float] = None) -> str:
    """Build the user-facing message for a failed external tool run.

    Deliberately omits the configured binary path. Where the tool is installed
    is a server detail that belongs in the log, not on a user's job page.
    """
    contact = (
        " If retrying does not solve it, contact Alan at alanrockefeller@gmail.com "
        "and include the job ID shown on this page."
    )
    if exit_code == EXIT_CODE_TOOL_NOT_FOUND:
        return (
            f"{tool_label} is not available on the server, so this step never "
            f"started. This is a server configuration problem, not a problem "
            f"with your sequences." + contact
        )
    if exit_code == EXIT_CODE_JOB_TIMEOUT:
        limit = f"{time_limit_hours:g}-hour " if time_limit_hours else ""
        return (
            f"{tool_label} was still running when this job's {limit}time limit "
            f"was reached, so it was stopped. Try a faster preset, fewer "
            f"bootstrap replicates, or fewer sequences." + contact
        )
    if exit_code == -24:
        return (
            f"{tool_label} used the full server CPU-time allowance and was "
            "stopped so the rest of Dikarya could stay available. Try again "
            "with MAFFT or FastTree, or reduce the number of sequences." + contact
        )
    if exit_code == -9:
        return (
            f"{tool_label} was stopped unexpectedly by the server. The usual "
            "causes are memory pressure, a job timeout, or a server restart." + contact
        )
    if exit_code == -11:
        return f"{tool_label} crashed unexpectedly (segmentation fault)." + contact
    if exit_code == -6:
        return (
            f"{tool_label} aborted, usually because it could not allocate enough memory."
            + contact
        )
    return f"{tool_label} failed with exit code {exit_code}." + contact


def _kill_child(process) -> None:
    """Kill a still-running child so a failed step leaves nothing behind.

    Without this, a job that timed out was marked failed while its tool kept
    running and burning CPU -- one RAxML run kept writing checkpoints for seven
    minutes after its job had already been reported as failed.
    """
    if not process or process.poll() is not None:
        return
    try:
        process.kill()
        process.wait(timeout=10)
    except Exception:
        logger.warning("Could not kill child process %s", getattr(process, "pid", "?"))


def _log_missing_executable(args: List[str], error: Exception) -> str:
    """Log the unresolvable path for operators, return a path-free summary."""
    logger.error(
        f"Executable not found, command never started: {args[0]!r} ({error}). "
        f"Check the *_BINARY setting in the environment file."
    )
    return f"[ERROR] {Path(args[0]).name} could not be started (not found on server)"


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

        command_args, limiter_used = _build_limited_argv(args)
        result = subprocess.run(
            command_args,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
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

        if limiter_used and result.returncode == 127 and "failed to execute" in stderr:
            return EXIT_CODE_TOOL_NOT_FOUND, stdout, _log_missing_executable(
                args, FileNotFoundError(stderr.strip())
            )
        return result.returncode, stdout, stderr

    except FileNotFoundError as e:
        return EXIT_CODE_TOOL_NOT_FOUND, "", _log_missing_executable(args, e)

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
    cpu_limit_seconds: Optional[int] = None,
    stderr_file_filter: Optional[callable] = None,
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
        stderr_file_filter: Optional predicate deciding whether a stderr line is
            worth persisting to stderr_path. Live streaming (on_stderr_line) and
            the error tail are unaffected, so a tool's per-iteration progress
            chatter can still drive the UI without being kept on disk forever.

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
            command_args, limiter_used = _build_limited_argv(
                args, cpu_limit_seconds
            )
            process = subprocess.Popen(
                command_args,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE if not stdout_file else stdout_file,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )
            
            # Stream stdout and stderr using select for non-blocking reads.
            # This also covers the stdout_path case: stdout then goes straight
            # to the file and process.stdout is None, so only stderr registers.
            # Draining through select (rather than a blocking `for line in
            # process.stderr`) is what lets the deadline below be enforced even
            # while the tool is producing no output at all.
            deadline = (start_time + timeout) if timeout else None

            import selectors

            sel = selectors.DefaultSelector()
            if process.stdout:
                sel.register(process.stdout, selectors.EVENT_READ, 'stdout')
            if process.stderr:
                sel.register(process.stderr, selectors.EVENT_READ, 'stderr')

            try:
                while sel.get_map():
                    if deadline and time.time() > deadline:
                        raise subprocess.TimeoutExpired(args, timeout)
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
                                keep = True
                                if stderr_file_filter:
                                    try:
                                        keep = stderr_file_filter(line)
                                    except Exception:
                                        keep = True
                                if keep:
                                    stderr_file.write(line + "\n")
                                    stderr_file.flush()

                            if on_stderr_line:
                                try:
                                    on_stderr_line(line)
                                except Exception:
                                    pass
            finally:
                sel.close()

            # Wait for the process itself. The pipes are drained by now, so this
            # normally returns at once; the remaining budget still bounds a tool
            # that closed its streams but has not exited.
            remaining = max(0.1, deadline - time.time()) if deadline else None
            process.wait(timeout=remaining)
            exit_code = process.returncode

            if (
                limiter_used
                and exit_code == 127
                and any("failed to execute" in line for line in stderr_tail_buffer)
            ):
                exit_code = EXIT_CODE_TOOL_NOT_FOUND

            signal_note = _describe_termination_signal(exit_code)
            if signal_note:
                logger.error(f"{signal_note} Command: {' '.join(args)}")
                stderr_tail_buffer.append(signal_note)
                if stderr_file:
                    stderr_file.write(signal_note + "\n")
            
        finally:
            if stdout_file:
                stdout_file.close()
            if stderr_file:
                stderr_file.write("-" * 40 + "\n")
                # `process` is always bound here but is None when Popen itself
                # raised. Dereferencing it then threw an AttributeError out of
                # the finally block, which replaced the real exception -- a
                # missing binary was reported as "'NoneType' has no attribute
                # 'returncode'" and the FileNotFoundError never reached a
                # handler that could explain it.
                stderr_file.write(f"Exit code: {process.returncode if process else 'N/A'}\n")
                stderr_file.close()
        
        stats["duration_seconds"] = time.time() - start_time
        stats["stderr_tail"] = list(stderr_tail_buffer)
        
        return exit_code, stats

    except FileNotFoundError as e:
        stats["duration_seconds"] = time.time() - start_time
        stats["stderr_tail"] = list(stderr_tail_buffer)
        stats["stderr_tail"].append(_log_missing_executable(args, e))
        return EXIT_CODE_TOOL_NOT_FOUND, stats

    except subprocess.TimeoutExpired:
        logger.error(f"Command timed out after {timeout}s: {args}")
        _kill_child(process)
        stats["duration_seconds"] = time.time() - start_time
        stats["stderr_tail"] = list(stderr_tail_buffer)
        stats["stderr_tail"].append(
            f"[TIMEOUT] {Path(args[0]).name} exceeded its {timeout}s time limit and was stopped."
        )
        return EXIT_CODE_JOB_TIMEOUT, stats

    except _JOB_TIMEOUT_EXCEPTIONS as e:
        # RQ's death penalty fires in this thread while the child is still
        # running. It subclasses Exception, so before this branch existed the
        # catch-all below swallowed it and returned a bare -1 -- and, because
        # only the TimeoutExpired branch killed anything, the tool was left
        # running unsupervised after the job was already marked failed.
        logger.error(f"Job time limit reached while running: {' '.join(args)} ({e})")
        _kill_child(process)
        stats["duration_seconds"] = time.time() - start_time
        stats["stderr_tail"] = list(stderr_tail_buffer)
        stats["stderr_tail"].append(
            f"[TIMEOUT] {Path(args[0]).name} was stopped when the job's time limit was reached."
        )
        return EXIT_CODE_JOB_TIMEOUT, stats

    except Exception as e:
        logger.exception(f"Exception running command: {args}")
        _kill_child(process)
        stats["duration_seconds"] = time.time() - start_time
        stats["stderr_tail"] = list(stderr_tail_buffer)
        stats["stderr_tail"].append(f"[ERROR] {str(e)}")
        return -1, stats
