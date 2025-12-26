import shutil
from pathlib import Path
from app.config import Config
from app.services.subprocess_utils import run_command

def run_trimming(input_alignment: Path, output_alignment: Path, trim_method: str, config: Config, logger) -> None:
    """
    Apply gap trimming to an alignment.
    trim_method options:
        - "none"
        - "trimal"      -> trimAl default settings
        - "bmge"        -> BMGE default settings
    """
    method = trim_method.lower()
    
    logger.info(f"Starting trimming with method: {method}")
    
    try:
        if method == "none" or not method:
            shutil.copy(input_alignment, output_alignment)
            logger.info("Trimming skipped (method='none'). Copied input to output.")
            return

        if method == "trimal":
            _run_trimal(input_alignment, output_alignment, config, logger)
        elif method == "bmge":
            _run_bmge(input_alignment, output_alignment, config, logger)
        else:
            # Fallback to none if unknown, or raise? 
            # Let's raise to be strict.
            raise ValueError(f"Unsupported trimming method: {method}")

        if not output_alignment.exists() or output_alignment.stat().st_size == 0:
             raise RuntimeError(f"Trimming failed: Output file {output_alignment} is missing or empty.")

        logger.info(f"Trimming completed successfully. Output: {output_alignment}")

    except Exception as e:
        logger.error(f"Trimming failed: {e}")
        raise

def _run_trimal(input_alignment: Path, output_alignment: Path, config: Config, logger):
    # trimal -in alignment_raw.fasta -out alignment_trimmed.fasta -automated1
    cmd = [
        config.TRIMAL_BINARY,
        "-in", str(input_alignment),
        "-out", str(output_alignment),
        "-automated1"
    ]
    
    log_file = output_alignment.parent.parent / "logs" / "alignment.log" # Log to alignment log
    returncode, stdout, stderr = run_command(cmd, log_file=log_file)
    
    if returncode != 0:
        raise RuntimeError(f"trimAl failed with return code {returncode}. See logs.")

def _run_bmge(input_alignment: Path, output_alignment: Path, config: Config, logger):
    # java -jar BMGE.jar -i alignment_raw.fasta -t DNA -of alignment_trimmed.fasta
    # Assuming config.BMGE_BINARY is the path to the jar or a wrapper script.
    # If it's a jar, we need "java", "-jar", config.BMGE_BINARY
    # If it's a wrapper, just config.BMGE_BINARY
    
    # Let's assume it's a wrapper or executable for now as per prompt "You may assume BMGE is installed as a binary".
    # But prompt also says "java -jar BMGE.jar ...".
    # I'll try to detect if it ends in .jar
    
    bmge_bin = config.BMGE_BINARY
    cmd = []
    
    if str(bmge_bin).endswith(".jar"):
        cmd = ["java", "-jar", bmge_bin]
    else:
        cmd = [bmge_bin]
        
    cmd.extend([
        "-i", str(input_alignment),
        "-t", "DNA",
        "-of", str(output_alignment)
    ])
    
    log_file = output_alignment.parent.parent / "logs" / "alignment.log"
    returncode, stdout, stderr = run_command(cmd, log_file=log_file)
    
    if returncode != 0:
        raise RuntimeError(f"BMGE failed with return code {returncode}. See logs.")
