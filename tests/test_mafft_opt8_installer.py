"""Safety checks for the root-run MAFFT opt8 installer."""

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "ops" / "mafft" / "install-mafft-opt8.sh"


def _source():
    return INSTALLER.read_text(encoding="utf-8")


def test_installer_is_valid_bash():
    result = subprocess.run(
        ["bash", "-n", str(INSTALLER)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_install_is_staged_until_after_validation():
    source = _source()
    install = 'make install PREFIX="$PREFIX" DESTDIR="$STAGE_ROOT"'
    validation = '[ "$status" -eq 0 ]'
    promotion = 'mv "$STAGED_PREFIX" "$PREFIX"'
    assert install in source
    assert source.index(install) < source.index(validation) < source.index(promotion)
    assert 'MAFFT_BINARIES="$STAGED_LIBEXEC"' in source


def test_self_test_requires_successful_commands_and_identical_output():
    source = _source()
    check = source.split("    check() {", 1)[1].split("    status=0", 1)[0]
    assert "|| true" not in check
    assert "ref_status=$?" in check
    assert "new_status=$?" in check
    assert '[ "$ref_status" -eq 0 ] && [ "$new_status" -eq 0 ]' in check
    assert 'cmp -s "$tmp/ref" "$tmp/new"' in check


def test_failed_promotion_restores_the_previous_prefix():
    source = _source()
    failure = source.split('if ! mv "$STAGED_PREFIX" "$PREFIX"; then', 1)[1]
    failure = failure.split("fi", 1)[0]
    assert 'mv "$BACKUP_PREFIX" "$PREFIX"' in failure
