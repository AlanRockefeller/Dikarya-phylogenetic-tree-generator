import re
import random
from pathlib import Path
from typing import Dict, Any, Tuple, Optional
from dataclasses import dataclass

# Constants
MIN_BOOTSTRAP_CAP = 100
MAX_BOOTSTRAP_CAP = 2000
DEFAULT_BOOTSTRAP_CAP = 500  # "Standard" preset cap

MAX_TOTAL_START_TREES = 40

# Presets Definitions
RUN_PRESETS = {
    'fast_good': {'pars': 2, 'rand': 0},
    'standard': {'pars': 5, 'rand': 0},
    'publication': {'pars': 10, 'rand': 10},
    'maximum': {'pars': 20, 'rand': 20},
}

BOOTSTRAP_PRESETS = {
    'standard': 500,
    'publication': 1000,
    'maximum': 2000,
}

# Supported Models (Base matrices)
VALID_DNA_MODELS = {
    'JC', 'K80', 'F81', 'HKY', 'TN93ef', 'TN93', 'K81', 'K81uf', 
    'TPM2', 'TPM2uf', 'TPM3', 'TPM3uf', 'TIM1', 'TIM1uf', 'TIM2', 'TIM2uf', 
    'TIM3', 'TIM3uf', 'TVMef', 'TVM', 'SYM', 'GTR', 'BIN', 'DNA010010',
    # Diploid / specialized models
    'GT10', 'GTGTR4', 'GTJC', 'GTHKY4', 'GTGTR', 
    'GT16', 'GT16JC', 'GT16GTR'
}

VALID_AA_MODELS = {
    m.upper() for m in {
        'Blosum62', 'cpREV', 'Dayhoff', 'DCMut', 'DEN', 'FLAVI', 'FLU', 'HIVb',
        'HIVw', 'JTT', 'JTT-DCMut', 'LG', 'mtART', 'mtINV', 'mtMAM', 'mtMET', 
        'mtREV', 'mtVER', 'mtZOA', 'PMB', 'Q.pfam', 'Q.bird', 'Q.insect', 
        'Q.mammal', 'Q.plant', 'Q.yeast', 'rtREV', 'stmtREV', 'VT', 'WAG', 
        'LG4M', 'LG4X', 'PROTGTR'
    }
}

# Regex for common modifiers (G, I, R, F, ASC, etc.)
# Matches:
# +G, +G4, +G[rate], +G{alpha}
# +R, +R4, +R{...}
# +F, +FC, +FO, +FE, +FU{...}
# +I, +IC, +IO, +IU{...}
# +ASC_LEWIS, +ASC_FELS{...}, +ASC_STAM{...}
# +M{...}
VALID_MODIFIER_REGEX = re.compile(
    r"^\+("
    r"G\d*[ma]?|"          # Gamma (G, G4, G4m, G4a)
    r"GA|"                 # Gamma median
    r"R\d*|"               # FreeRate (R, R4)
    r"F[COEU]?|"           # Frequencies (F, FC, FO, FE, FU)
    r"I[COU]?|"            # Invariant (I, IO, IC, IU)
    r"ASC_(LEWIS|FELS|STAM)|" # Ascertainment
    r"M[i]?"               # Character mapping
    r")(\{[^{}]+\}){0,2}$"  # Optional values; FreeRate / mappings may use two blocks
)

_MODEL_PARAM_NUMBER = re.compile(
    r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$"
)
_MAX_MODEL_PARAM_LENGTH = 512
_MULTISTATE_MODEL = re.compile(r"^MULTI(\d+)_(?:MK|GTR)$")


def _is_safe_model_param(value: str) -> bool:
    """Validate the numeric contents of one RAxML-NG ``{...}`` block.

    RAxML-NG uses brace blocks for scalar values and numeric vectors, including
    gamma alpha values, substitution-rate vectors, base frequencies, and
    ascertainment counts. Vector elements are separated by ``/`` in canonical
    RAxML-NG output; commas are also accepted here for model forms supported by
    its parser. Commands use argv, so this check is model-syntax hygiene rather
    than shell escaping. In particular, it deliberately allows signed values,
    decimals, and scientific notation while rejecting paths, nested braces,
    empty vector elements, and control characters.
    """
    if not value or len(value) > _MAX_MODEL_PARAM_LENGTH:
        return False
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        return False
    return all(
        _MODEL_PARAM_NUMBER.fullmatch(item) is not None
        for item in re.split(r"[/,]", value)
    )


def _is_safe_mapping_param(value: str) -> bool:
    """Validate one inline RAxML-NG +M/+Mi character-map block.

    The upstream parser treats these as sequences of single-byte state symbols,
    not numeric vectors. Keep the accepted syntax to non-whitespace printable
    ASCII and exclude braces, which delimit the parameter itself. Semantic
    checks (state count, uniqueness, and state/gap overlap) are performed by
    _is_valid_mapping_params below.
    """
    return bool(value) and len(value) <= 126 and all(
        33 <= ord(char) <= 126 and char not in "{}" for char in value
    )


def _is_valid_mapping_params(
    values: list[str], base_name: str, *, case_sensitive: bool
) -> bool:
    """Validate inline custom states and optional gap symbols for +M / +Mi."""
    model_match = _MULTISTATE_MODEL.fullmatch(base_name)
    if not model_match or len(values) not in (1, 2):
        return False
    if not all(_is_safe_mapping_param(value) for value in values):
        return False

    states = values[0]
    gaps = values[1] if len(values) == 2 else ""
    # With only one block, upstream interprets its contents as a character-map
    # file whenever that path exists. Dikarya accepts inline maps only: a
    # user-configured model must not turn into an implicit server-side file
    # read merely because its state symbols happen to name a local file.
    if not gaps:
        try:
            if Path(states).is_file():
                return False
        except OSError:
            return False
    normalize = (lambda char: char) if case_sensitive else (lambda char: char.upper())
    normalized_states = [normalize(char) for char in states]
    normalized_gaps = [normalize(char) for char in gaps]

    # corax_util_charmap_create expects exactly one symbol for every MULTI<n>
    # state, without duplicate states or gap aliases and without overlap.
    if len(states) != int(model_match.group(1)):
        return False
    if len(set(normalized_states)) != len(normalized_states):
        return False
    if len(set(normalized_gaps)) != len(normalized_gaps):
        return False
    return set(normalized_states).isdisjoint(normalized_gaps)

def _normalize_raxml_model(model_str: str) -> str:
    """
    Normalize RAxML model string.
    - Removes whitespace outside brace-enclosed parameter values.
    - Removes trailing commas.
    - Fixes spaced partition assignments (e.g., ", noname = " -> ",noname=").

    Whitespace inside a parameter block is preserved so validation rejects it;
    silently deleting characters from a scientific value could turn malformed
    input into a different, apparently valid model.
    """
    if not model_str:
        return ""
    
    normalized = []
    depth = 0
    for char in model_str:
        if char == "{":
            depth += 1
        if not char.isspace() or depth > 0:
            normalized.append(char)
        if char == "}":
            depth -= 1
    s = "".join(normalized)
    
    # Remove trailing commas
    while s.endswith(","):
        s = s[:-1]
        
    return s

def _split_model_string(model_str: str, delimiter: str = ',') -> list[str]:
    """
    Split model string by delimiter, respecting braces {...}.
    Commas inside braces (e.g. inside a map or generic parameter) are ignored.
    """
    parts = []
    current = ""
    depth = 0
    
    for char in model_str:
        if char == '{':
            depth += 1
            current += char
        elif char == '}':
            depth -= 1
            current += char
        elif char == delimiter and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
            
    if current:
        parts.append(current)
    elif parts: # Case where string ends with delimiter?
        parts.append("")
        
    return parts

@dataclass
class ResolvedRaxmlParams:
    """Validated and resolved parameters ready for command generation."""
    enable_bootstrap: bool
    bootstrap_cap: int
    start_tree_spec: str
    model: str
    seed: int
    data_type: str  # DNA or AA
    warnings: list[str]
    outgroup: Optional[str] = None
    enable_moose: bool = False
    enable_early_stopping: bool = False


def validate_and_resolve_raxml_params(params: Dict[str, Any], data_type: str = 'DNA') -> ResolvedRaxmlParams:
    """
    Validate raw input parameters and resolve them to safe, executable values.
    
    Args:
        params: Raw dictionary from client request (TreeBuilderParams fields).
        data_type: 'DNA' or 'AA' (detected from MSA usually).
        
    Returns:
        ResolvedRaxmlParams object with safe values and warnings list.
    """
    warnings = []

    # 1. Bootstrapping Logic
    enable_bootstrap = bool(params.get('enable_bootstrap', True))  # Default True matches legacy behavior somewhat
    bootstrap_cap = DEFAULT_BOOTSTRAP_CAP
    
    if enable_bootstrap:
        # Determine requested cap
        req_cap = None
        
        # Check custom cap first (if provided and valid)
        if params.get('bootstrap_cap') is not None:
             try:
                 req_cap = int(params['bootstrap_cap'])
             except (ValueError, TypeError):
                 warnings.append("Invalid bootstrap_cap provided; using preset default.")
                 pass
        
        # Fallback to preset if no custom cap
        if req_cap is None:
            bs_preset = params.get('bootstrap_preset', 'standard')
            req_cap = BOOTSTRAP_PRESETS.get(bs_preset, DEFAULT_BOOTSTRAP_CAP)

        # Clamp
        if req_cap < MIN_BOOTSTRAP_CAP:
            warnings.append(f"Requested bootstrap cap {req_cap} too low; clamped to {MIN_BOOTSTRAP_CAP}.")
            bootstrap_cap = MIN_BOOTSTRAP_CAP
        elif req_cap > MAX_BOOTSTRAP_CAP:
            warnings.append(f"Requested bootstrap cap {req_cap} exceeded limit; clamped to {MAX_BOOTSTRAP_CAP}.")
            bootstrap_cap = MAX_BOOTSTRAP_CAP
        else:
            bootstrap_cap = req_cap
    else:
        bootstrap_cap = 0  # Irrelevant but safe value

    # 2. Starting Trees Logic
    run_preset = params.get('run_preset', 'fast_good')
    preset_trees = RUN_PRESETS.get(run_preset, RUN_PRESETS['fast_good'])
    
    pars_n = preset_trees['pars']
    rand_n = preset_trees['rand']

    # Check override
    override_str = params.get('start_tree_override')
    if override_str:
        # Expected format: "pars{N}" or "pars{N},rand{M}" or "rand{M}"
        # Parse flexible inputs
        p_match = re.search(r'pars\{(\d+)\}', override_str)
        r_match = re.search(r'rand\{(\d+)\}', override_str)
        
        if p_match or r_match:
            try:
                ov_pars = int(p_match.group(1)) if p_match else 0
                ov_rand = int(r_match.group(1)) if r_match else 0
                
                if ov_pars + ov_rand == 0:
                     warnings.append("Start tree override resulted in 0 trees; ignored.")
                elif ov_pars + ov_rand > MAX_TOTAL_START_TREES:
                     warnings.append(f"Requested total start trees ({ov_pars + ov_rand}) exceeds limit ({MAX_TOTAL_START_TREES}); using preset '{run_preset}' instead.")
                else:
                    pars_n = ov_pars
                    rand_n = ov_rand
            except ValueError:
                warnings.append("Failed to parse start tree override; using preset.")
        else:
             warnings.append("Invalid start tree override format; using preset.")

    # Construct validated spec
    specs = []
    if pars_n > 0:
        specs.append(f"pars{{{pars_n}}}")
    if rand_n > 0:
        specs.append(f"rand{{{rand_n}}}")
    start_tree_spec = ",".join(specs)
    if not start_tree_spec:
        # Fallback safety
        start_tree_spec = "pars{2}"

    # 3. Model Logic
    model = params.get('model', '').strip()
    enable_moose = bool(params.get('moose_enabled', False))
    if model and any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in model):
        warnings.append("Model string contained control characters; reverting to default.")
        model = "GTR+G" if data_type == "DNA" else "LG+G"
    
    if not model and not enable_moose:
        # Default defaults
        model = "GTR+G" if data_type == "DNA" else "LG+G"

    if model:
        # Support Partitioned Models: "Model, Part=Range, ..."
        # 1. Split top-level commas
        raw_parts = _split_model_string(model, ',')
        
        validated_parts = []
        model_valid = True
        
        for part in raw_parts:
            # Normalize part (remove spaces)
            part_norm = _normalize_raxml_model(part)
            
            if not part_norm:
                continue
                
            # Check if it's a Partition Definition (contains '=')
            # Example: part1=1-100 or gene1 = 1-500/2
            # But wait, modifiers don't usually have = outside braces.
            # Base models don't either.
            if '=' in part_norm:
                 # Clean validation for partition assignment
                 # part_name = range_spec
                 # Split on first =
                 p_name, p_range = part_norm.split('=', 1)
                 
                 # Validate Name
                 if not re.match(r"^[A-Za-z0-9_]+$", p_name):
                      warnings.append(f"Invalid partition name '{p_name}'.")
                      model_valid = False
                 
                 # Validate Range spec
                 # Allow digits, dash, comma, slash (codons)
                 if not re.match(r"^[0-9\-\,\/]+$", p_range):
                      warnings.append(f"Invalid partition range '{p_range}'.")
                      model_valid = False
                      
                 validated_parts.append(part_norm)
                 continue

            # Identify if this is a Model string (Base + Modifiers)
            # 1. Parse into Base + Modifiers
            model_parts = []
            current = ""
            depth = 0
            for char in part_norm:
                if char == '{': depth += 1
                elif char == '}': depth -= 1
                
                if char == '+' and depth == 0:
                    model_parts.append(current)
                    current = ""
                else:
                    current += char
            model_parts.append(current) 
            
            base_part = model_parts[0]
            modifiers = model_parts[1:]
            
            # 2. Validate Base
            base_name_match = re.fullmatch(r"([A-Za-z0-9_\.]+)(\{[^{}]+\})?", base_part)
            
            part_is_valid = True
            
            if not base_name_match:
                 warnings.append(f"Invalid model component '{base_part}'.")
                 part_is_valid = False
            else:
                 base_name = base_name_match.group(1).upper()
                 base_param = base_name_match.group(2)
                 
                 valid_set = VALID_DNA_MODELS if data_type == "DNA" else VALID_AA_MODELS
                 is_multi = base_name.startswith("MULTI") and ("_MK" in base_name or "_GTR" in base_name)
                 
                 if base_name not in valid_set and not is_multi:
                      warnings.append(f"Unknown substitution model '{base_name}'.")
                      part_is_valid = False
                      
                 if base_param and not _is_safe_model_param(base_param[1:-1]):
                      warnings.append(f"Base parameter '{base_param}' ignored/unsafe.")
                      part_is_valid = False

            # 3. Validate Modifiers
            if part_is_valid:
                for mod in modifiers:
                    mod_str = "+" + mod 
                    modifier_match = VALID_MODIFIER_REGEX.fullmatch(mod_str)
                    if not modifier_match:
                         warnings.append(f"Unknown modifier '{mod_str}'.")
                         part_is_valid = False
                    else:
                         modifier_name = modifier_match.group(1)
                         param_values = re.findall(r"\{([^{}]+)\}", mod)
                         if modifier_name in ("M", "Mi"):
                             if not _is_valid_mapping_params(
                                 param_values,
                                 base_name,
                                 case_sensitive=modifier_name == "M",
                             ):
                                 warnings.append(f"Character mapping '{mod_str}' is invalid.")
                                 part_is_valid = False
                         else:
                             # Only FreeRate accepts both a rates block and a
                             # weights block; every other modifier is numeric
                             # but has at most one parameter block.
                             if len(param_values) > 1 and not modifier_name.startswith("R"):
                                 warnings.append(f"Modifier '{mod_str}' has too many parameter blocks.")
                                 part_is_valid = False
                             for value in param_values:
                                 if not _is_safe_model_param(value):
                                     warnings.append(f"Modifier param '{{{value}}}' unsafe.")
                                     part_is_valid = False
            
            if part_is_valid:
                validated_parts.append(part_norm)
            else:
                model_valid = False
                break # Stop checking if any part fails

        if not model_valid:
             warnings.append(f"Model string rejected; reverting to default.")
             model = "GTR+G" if data_type == "DNA" else "LG+G"
        else:
             # Reconstruct normalized string
             model = ",".join(validated_parts)
    
    # 4. Seed Logic
    seed = params.get('seed')
    if seed is None:
        seed = random.randint(1, 2147483647)
    else:
        try:
            seed = int(seed)
            # Enforce 32-bit signed range roughly, but avoid INT_MIN
            if not (-2147483647 <= seed <= 2147483647):
                 warnings.append("Seed out of valid range; using random seed.")
                 seed = random.randint(1, 2147483647)
        except (ValueError, TypeError):
             warnings.append("Invalid seed provided; using random seed.")
             seed = random.randint(1, 2147483647)

    # 5. Outgroup Logic
    outgroup = params.get('outgroup')
    if outgroup:
        outgroup = str(outgroup).strip()
        # Reject whitespace/quotes
        if any(ch.isspace() for ch in outgroup) or '"' in outgroup or "'" in outgroup:
             warnings.append("Outgroup contained whitespace or quotes; ignored.")
             outgroup = None
        # Strict char check. Narrowed from a previous regex that allowed
        # `|`, `:`, and `@` -- none of those appear in real FASTA tip IDs
        # for our pipeline, and removing them reduces the surface for any
        # taxa-name confusion attempts. Argv passing already prevents shell
        # injection; this is hygiene.
        elif not re.match(r"^[A-Za-z0-9_.\-]+$", outgroup):
             warnings.append(f"Outgroup '{outgroup}' contains invalid characters; ignored.")
             outgroup = None

    # 6. Early Stopping
    # NOTE: Does NOT check if supported by binary here. That check happens in service.
    enable_early_stopping = bool(params.get('enable_early_stopping', params.get('early_stopping', False)))
    
    # Force default OFF for high-quality presets
    if run_preset in ('publication', 'maximum') and enable_early_stopping:
        warnings.append(f"Early stopping disabled for '{run_preset}' quality to ensure thorough search.")
        enable_early_stopping = False

    return ResolvedRaxmlParams(
        enable_bootstrap=enable_bootstrap,
        bootstrap_cap=bootstrap_cap,
        start_tree_spec=start_tree_spec,
        model=model,
        seed=seed,
        data_type=data_type,
        warnings=warnings,
        outgroup=outgroup,
        enable_moose=enable_moose,
        enable_early_stopping=enable_early_stopping
    )
