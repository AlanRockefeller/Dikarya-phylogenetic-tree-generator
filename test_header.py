import re
from typing import Dict, List

def _build_header(record: Dict) -> str:
    acc_ver = record.get("version") or record.get("accession")
    organism = record.get("organism") or "(unknown organism)"
    
    parts = [acc_ver, organism]
    
    def _strip_organism(val: str, org_name: str) -> str:
        if not val:
            return val
        val = " ".join(str(val).split())
        org_name = " ".join(str(org_name or "").split())
        org_tokens = org_name.split()
        org_binomial = " ".join(org_tokens[:2]) if len(org_tokens) >= 2 else ""
        variants = [v for v in [org_name, org_binomial] if v]
        for v in variants:
            val = re.sub(re.escape(v), "", val, flags=re.IGNORECASE)
        val = re.sub(r"\s*[:;,()\[\]{}]\s*", " ", val)
        val = re.sub(r"\bof\b", "", val, flags=re.IGNORECASE)
        val = " ".join(val.split()).strip()
        for v in variants:
            val = re.sub(rf"(?:\s+{re.escape(v)})+\s*$", "", val, flags=re.IGNORECASE).strip()
        return val

    def _drop_trailing_organism_tokens(tokens: List[str], org_name: str) -> List[str]:
        if not tokens or not org_name:
            return tokens
        org_tokens = org_name.split()
        n = len(org_tokens)
        if len(tokens) >= n and [t.lower() for t in tokens[-n:]] == [t.lower() for t in org_tokens]:
            start_index_of_match = len(tokens) - n
            if start_index_of_match <= 1:
                return tokens
            return tokens[:-n]
        return tokens

    TYPE_KEYWORDS = ["holotype", "isotype", "epitype", "lectotype", "neotype", "paratype", "syntype", "topotype"]

    def _extract_type_status(rec: Dict) -> List[str]:
        t_val = rec.get("type_material")
        if t_val:
            t_val_norm = " ".join(str(t_val).split())
            t_val_norm = _strip_organism(t_val_norm, organism)
            low = t_val_norm.lower()
            found = [kw for kw in TYPE_KEYWORDS if re.search(rf"\b{re.escape(kw)}\b", low)]
            if found:
                return found
            return [t_val_norm] if t_val_norm else []
        blob = (rec.get("blob") or "").lower()
        found = [kw for kw in TYPE_KEYWORDS if re.search(rf"\b{re.escape(kw)}\b", blob)]
        return found

    quals = record.get("source_features", {})
    sample_id = None
    for key in ["specimen_voucher", "culture_collection", "bio_material", "isolate", "strain"]:
        if val := quals.get(key):
            val = _strip_organism(val, organism)
            if val:
                sample_id = f"{key} {val}"
                break
            
    if sample_id:
        parts.append(sample_id)
        
    extras = []
    current_len = sum(len(p) for p in parts) + len(parts)
    MAX_LEN = 120
    
    for key in ["geo_loc_name", "country"]:
        if val := quals.get(key):
            val = " ".join(val.split())
            val = _strip_organism(val, organism)
            if not val:
                 continue
            if key in ["geo_loc_name", "country"]:
                token = f"{val}"
            else:
                token = f"{key}=\"{val}\""
            if len(extras) < 3 and (current_len + len(token)) < (current_len + 120):
                extras.append(token)
                current_len += len(token) + 1
                
    if extras:
        parts.extend(extras)
        
    type_status = _extract_type_status(record)
    if type_status:
        parts.extend(type_status)
        
    parts = _drop_trailing_organism_tokens(parts, organism)
        
    return ">" + " ".join(parts)

record1 = {
    "accession": "PP959535",
    "version": "PP959535",
    "organism": "Strobilomyces sp.",
    "source_features": {}
}
print(repr(_build_header(record1)))

record2 = {
    "accession": "PP959535",
    "version": "PP959535",
    "organism": "",
    "source_features": {}
}
print(repr(_build_header(record2)))
