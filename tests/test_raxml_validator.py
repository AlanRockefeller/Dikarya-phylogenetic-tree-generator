import pytest

from app.services.raxml_validator import (
    _is_safe_mapping_param,
    _is_safe_model_param,
    validate_and_resolve_raxml_params,
)


@pytest.mark.parametrize(
    "model",
    [
        "GTR+G{0.5}",
        "GTR{1/2/3/4/5/6}+G",
        "GTR+FU{0.1/0.2/0.3/0.4}+G",
        "GTR{1e-3/+2.5/.75/4/5/6}+G",
        "GTR{0.1/0.5/1/2/3.5/0.5}+R3+FC",
        "GTR+R4{1/2/3/4}{0.1/0.2/0.3/0.4}",
    ],
)
def test_parameterized_dna_models_are_accepted(model):
    resolved = validate_and_resolve_raxml_params({"model": model})
    assert resolved.model == model
    assert not any("rejected" in warning for warning in resolved.warnings)


@pytest.mark.parametrize("value", ["0.5", "1/2.5/3e-4", "1,2,3.0"])
def test_model_parameter_numeric_forms_are_safe(value):
    assert _is_safe_model_param(value)


@pytest.mark.parametrize(
    "model",
    [
        "MULTI6_GTR+M{AbCDef}{X-?}",
        "MULTI5_MK+Mi{AbCdF}",
    ],
)
def test_multistate_character_mappings_are_accepted(model):
    resolved = validate_and_resolve_raxml_params({"model": model})
    assert resolved.model == model
    assert not any("rejected" in warning for warning in resolved.warnings)


@pytest.mark.parametrize("value", ["AbCDef", "X-?", "01!#%&"])
def test_character_mapping_parameter_syntax_is_safe(value):
    assert _is_safe_mapping_param(value)


@pytest.mark.parametrize(
    "model",
    [
        "GTR+G{0.5",
        "GTR+G0.5}",
        "GTR+G{{0.5}}",
        "GTR+G{0.5//1}",
        "GTR+G{fast}",
        "GTR+G{0. 5}",
        "GTR+G{../../etc/passwd}",
        "GTR+G{0.5\x00/1}",
        "GTR+\nG{0.5}",
        "MULTI6_GTR+M{AbCDef}{X-?",
        "MULTI6_GTR+M{{AbCDef}}{X-?}",
        "MULTI6_GTR+M{AbCDe}",
        "MULTI6_GTR+M{AbCDeA}",
        "MULTI6_GTR+M{AbCDef}{A-?}",
        "MULTI6_GTR+M{AbCDef}{X-\x00}",
        "MULTI5_MK+Mi{AaCdF}",
    ],
)
def test_malformed_or_control_character_models_fall_back(model):
    resolved = validate_and_resolve_raxml_params({"model": model})
    assert resolved.model == "GTR+G"
    assert resolved.warnings


def test_file_backed_character_mapping_is_not_accepted(tmp_path, monkeypatch):
    (tmp_path / "AbCDef").write_text("server-side mapping data")
    monkeypatch.chdir(tmp_path)
    resolved = validate_and_resolve_raxml_params({
        "model": "MULTI6_GTR+M{AbCDef}",
    })
    assert resolved.model == "GTR+G"
    assert resolved.warnings


def test_moose_does_not_bypass_valid_configured_model_validation():
    resolved = validate_and_resolve_raxml_params({
        "model": "GTR{1/2/3/4/5/6}+G{0.5}",
        "moose_enabled": True,
    })
    assert resolved.model == "GTR{1/2/3/4/5/6}+G{0.5}"


def test_moose_invalid_configured_model_uses_validated_dna_fallback():
    resolved = validate_and_resolve_raxml_params({
        "model": "NOT_A_MODEL",
        "moose_enabled": True,
    })
    assert resolved.model == "GTR+G"
    assert any("rejected" in warning for warning in resolved.warnings)


def test_moose_invalid_configured_model_uses_amino_acid_fallback():
    resolved = validate_and_resolve_raxml_params(
        {"model": "GTR+G", "moose_enabled": True}, data_type="AA"
    )
    assert resolved.model == "LG+G"


def test_moose_valid_amino_acid_model_is_retained_as_fallback():
    resolved = validate_and_resolve_raxml_params(
        {"model": "WAG+G{0.5}", "moose_enabled": True}, data_type="AA"
    )
    assert resolved.model == "WAG+G{0.5}"


# ---------------------------------------------------------------------------
# The base-name grammar has to admit every name on the allowlist.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model",
    [
        # RAxML-NG's own JTT-DCMut matrix. The base-name character class did not
        # include "-", so the explicitly requested model failed to parse and was
        # silently replaced by the default.
        "JTT-DCMut",
        "JTT-DCMut+G",
        "JTT-DCMut+G{0.5}+F",
        # ... and the two other punctuation forms already on the allowlist.
        "Q.pfam+G",
        "LG4X",
    ],
)
def test_allowlisted_amino_acid_models_are_accepted_verbatim(model):
    resolved = validate_and_resolve_raxml_params({"model": model}, data_type="AA")

    assert resolved.model == model
    assert not any("Unknown substitution model" in w for w in resolved.warnings)
    assert not any("Invalid model component" in w for w in resolved.warnings)


def test_every_allowlisted_model_name_parses():
    """No allowlisted name may be unreachable through the model parser.

    This is how the second half of the same defect surfaced: the parser
    compares the *uppercased* base name against the allowlist, and the DNA set
    was the one set not uppercased -- so K81uf, TN93ef, TVMef and the five
    other mixed-case entries were rejected by their own allowlist and quietly
    replaced with GTR+G.
    """
    from app.services.raxml_validator import VALID_AA_MODELS, VALID_DNA_MODELS

    for data_type, names in (("AA", VALID_AA_MODELS), ("DNA", VALID_DNA_MODELS)):
        for name in sorted(names):
            resolved = validate_and_resolve_raxml_params(
                {"model": name}, data_type=data_type
            )
            assert resolved.model == name, (data_type, name, resolved.warnings)


@pytest.mark.parametrize("model", ["K81uf", "TN93ef", "TVMef", "TIM2uf+G"])
def test_mixed_case_dna_models_are_not_silently_substituted(model):
    resolved = validate_and_resolve_raxml_params({"model": model})

    assert resolved.model == model
    assert not any("Unknown substitution model" in w for w in resolved.warnings)


def test_a_hyphen_does_not_admit_an_unknown_model():
    """Widening the character class must not widen what is accepted."""
    resolved = validate_and_resolve_raxml_params(
        {"model": "NOT-A-MODEL"}, data_type="AA"
    )

    assert resolved.model == "LG+G"
    assert any("Unknown substitution model" in w for w in resolved.warnings)
