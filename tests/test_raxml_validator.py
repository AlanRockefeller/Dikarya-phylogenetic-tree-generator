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
