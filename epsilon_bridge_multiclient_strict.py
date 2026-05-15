# -*- coding: utf-8 -*-
"""
epsilon_bridge_multiclient_strict.py (strict, robust client_db discovery)

- Strict exact-rate account resolution:
    account_{category}_fpa_kat_{rate}%  (accept legacy account_{category}_{rate}% for SAME rate only)
- Receipts χωρίς ανάλυση & εγγυοδοσία -> forced 0%
- Reason rules:
    * Receipts: "Name_issuer (AFM_issuer)" when available. If only one exists, use that. Else "Απόδειξη AA".
    * Invoices: issuer name (or name from client_db by AFM) else "Τιμολόγιο AA" / provided reason.
- Robust client_db discovery: finds client_db file "χύμα" μέσα στο data/, even if named differently.
"""
from __future__ import annotations

import os, re, json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from activity_monitor import monitor_resources

# ----------------------- basic utils -----------------------
def _digits(s: Any) -> str:
    return "".join(ch for ch in str(s or "") if ch.isdigit())

def _norm_afm(val: Any) -> str:
    d = _digits(val)
    if not d:
        return ""
    if len(d) >= 9:
        d = d[-9:]
    return d.zfill(9)

def _safe_json_read(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default

def _to_date(d: Any) -> Optional[datetime]:
    if not d:
        return None
    s = str(d).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%y"):
        try:
            return datetime.strptime(s[:10], fmt)
        except Exception:
            pass
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None

def _ddmmyyyy(d: Any) -> str:
    dt = _to_date(d)
    return dt.strftime("%d/%m/%Y") if dt else ""

def _safe_int(x: Any) -> Optional[int]:
    try:
        return int(float(str(x).strip()))
    except Exception:
        return None

def _round2(x: Any) -> float:
    try:
        return round(float(str(x).replace(",", ".")), 2)
    except Exception:
        return 0.0

# ----------------------- settings helpers -----------------------
def _norm_key(s: Any) -> str:
    s = str(s or "")
    s = s.replace("％", "%")
    s = re.sub(r"\s+", "", s)
    s = s.lower().replace("__", "_")
    return s

def _settings_norm(settings: Dict[str, Any]) -> Dict[str, Any]:
    return {_norm_key(k): v for k, v in (settings or {}).items()}

def _split_account_value(val: Any) -> Tuple[str, Optional[int]]:
    s = str(val or "").strip()
    m = re.match(r"^(.*)_(\d{1,2})$", s)
    if m:
        base = m.group(1).strip()
        try:
            vr = int(m.group(2))
        except Exception:
            vr = None
        return base, vr
    return s, None

def _account_key_candidates(canon: str, rate: int) -> List[str]:
    r = str(int(rate))
    base = f"account_{canon}_"
    return [
        _norm_key(base + f"fpa_kat_{r}%"),
        _norm_key(base + f"fpa_kat_{r}"),
        _norm_key(base + f"{r}%"),
        _norm_key(base + f"{r}"),
    ]

def _get_account_for(settings: Dict[str, Any], canon: str, rate: int) -> str:
    setts = _settings_norm(settings)
    for key in _account_key_candidates(canon, rate):
        val = setts.get(key, "")
        if isinstance(val, str) and val.strip():
            base, _ = _split_account_value(val)
            return base
    return ""

def _account_header_P(settings: Dict[str, Any], is_receipt: bool) -> str:
    setts = _settings_norm(settings)
    key = _norm_key("account_supplier_retail" if is_receipt else "account_supplier_wholesale")
    val = setts.get(key, "")
    if isinstance(val, str) and val.strip():
        base, _ = _split_account_value(val)
        return base
    return ""


def _merge_custom_accounts(settings: Dict[str, Any], credential: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(settings or {})
    if not isinstance(credential, dict):
        return merged
    custom_cats = credential.get("custom_categories")
    if not isinstance(custom_cats, list):
        return merged
    for item in custom_cats:
        if not isinstance(item, dict):
            continue
        accounts = item.get("accounts") or {}
        applies_receipts = bool(item.get("applies_to_receipts"))
        if isinstance(accounts, dict) and not applies_receipts:
            applies_receipts = bool(accounts.get("__applies_to_receipts"))
        enabled = bool(item.get("enabled"))
        if not (enabled or applies_receipts):
            continue
        slug = str(item.get("id") or item.get("slug") or "").strip()
        if not slug:
            continue
        for rate_key, code in accounts.items():
            code_str = str(code).strip()
            if not code_str:
                continue
            rate_norm = re.sub(r"[^0-9]", "", str(rate_key))
            if not rate_norm:
                continue
            key = f"account_{slug}_fpa_kat_{rate_norm}%"
            merged[key] = code_str
    return merged

# ----------------------- category + VAT inference -----------------------
def _canon_category(raw: str) -> str:
    s = (raw or "").strip().lower().replace(" ", "_")
    if "αποδειξ" in s:
        return "αποδειξακια"
    if "εγγυοδοσι" in s:
        return "εγγυοδοσια"
    if "αμοιβ" in s and "τριτ" in s:
        return "αμοιβες_τριτων"
    if "χωρι" in s and "φπα" in s:
        return "δαπανες_χωρις_φπα"
    if "α_υλων" in s or "α'_υλων" in s or "πρωτ" in s or "υλων" in s:
        return "αγορες_α_υλων"
    if "γενικ" in s or "δαπαν" in s or "εξοδ" in s:
        return "γενικες_δαπανες_με_φπα"
    if "εμπορευ" in s or ("αγορ" in s and "εμπ" in s):
        return "αγορες_εμπορευματων"
    return s

_VAT_PATTERNS = [
    r"(\d{1,2})\s*%",
    r"φπα\s*(\d{1,2})",
    r"vat\s*(\d{1,2})",
    r"tax\s*rate\s*(\d{1,2})",
]

def _extract_rate_from_string(s: Any) -> Optional[int]:
    if s is None:
        return None
    text = str(s).lower()
    for pat in _VAT_PATTERNS:
        m = re.search(pat, text)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None

def _infer_vat_rate_for_line(ln: Dict[str, Any], rec: Dict[str, Any]) -> Tuple[Optional[int], str]:
    for fld in ("vat_rate", "vatrate", "vatRate", "ΦΠΑ_rate", "fpa_rate"):
        val = ln.get(fld)
        if val not in (None, ""):
            try:
                return int(round(float(str(val).replace(",", ".")))), f"line.{fld}"
            except Exception:
                pass
    rate = _extract_rate_from_string(ln.get("vat_category")) or _extract_rate_from_string(ln.get("vatCategory"))
    if rate is not None:
        return rate, "line.vat_category"
    rate = _extract_rate_from_string(ln.get("category"))
    if rate is not None:
        return rate, "line.category"
    try:
        net = float(str(ln.get("amount", ln.get("net") or 0)).replace(",", "."))
    except Exception:
        net = 0.0
    try:
        vat = float(str(ln.get("vat", ln.get("vat_amount") or 0)).replace(",", "."))
    except Exception:
        vat = 0.0
    # If we have a net amount, treat explicit zero vat as 0%.
    # Previously the logic only computed rate when both net and vat were truthy,
    # which skipped cases where vat==0 (0% VAT). This caused unresolved account
    # errors for 0%-tax lines. Handle vat==0 explicitly.
    if net:
        try:
            if abs(vat) < 1e-9:
                return 0, "calc"
            if vat:
                return int(round((vat / net) * 100.0)), "calc"
        except Exception:
            pass
    rate = _extract_rate_from_string(rec.get("vatCategory")) or _extract_rate_from_string(rec.get("type"))
    if rate is not None:
        return rate, "rec.hint"
    try:
        rnet = float(str(rec.get("totalNetValue") or 0).replace(",", "."))
        rvat = float(str(rec.get("totalVatAmount") or 0).replace(",", "."))
        if rnet and rvat:
            return int(round((rvat / rnet) * 100.0)), "rec.calc"
    except Exception:
        pass
    return None, "unknown"


def _is_receipt(rec: Dict[str, Any]) -> bool:
    # Local helper to avoid NameError even if global helper is missing
    def _strip_diacritics_local(text: str) -> str:
        try:
            import unicodedata as _ud
            if not text:
                return ""
            return "".join(ch for ch in _ud.normalize("NFD", str(text)) if _ud.category(ch) != "Mn")
        except Exception:
            return str(text or "")
    # choose helper
    try:
        proc = _strip_diacritics  # type: ignore[name-defined]
    except Exception:
        proc = _strip_diacritics_local

    t_raw = f"{rec.get('type','')} {rec.get('category','')}"
    t = proc(t_raw).lower()
    keys = ("receipt","apodeix","apodeixi","apod","αποδειξ","αποδειξη","λιαν","λιανικη")
    return any(k in t for k in keys)


def _receipt_analysis_enabled(rec: Dict[str, Any]) -> bool:
    r = rec or {}
    return bool(
        r.get("receipt_analysis_enabled")
        or r.get("receipts_analysis_enabled")
        or r.get("receiptAnalysisEnabled")
        or r.get("analysis_receipts")
    )

def _parse_lines(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    lines = rec.get("lines") or rec.get("invoice_lines") or rec.get("details") or []
    out: List[Dict[str, Any]] = []
    if isinstance(lines, list) and lines:
        for ln in lines:
            net = _round2(ln.get("amount", ln.get("net") or 0))
            vat = _round2(ln.get("vat", ln.get("vat_amount") or 0))
            vr, src = _infer_vat_rate_for_line(ln, rec)
            cat = _canon_category(ln.get("category") or "")
            out.append({"net": float(net), "vat": float(vat), "vat_rate": vr, "vat_src": src, "category": cat})
        return out
    net = _round2(rec.get("totalNetValue", 0))
    vat = _round2(rec.get("totalVatAmount", 0))
    vr, src = _infer_vat_rate_for_line({}, rec)
    cat = _canon_category(rec.get("category", "") or "")
    return [{"net": float(net), "vat": float(vat), "vat_rate": vr, "vat_src": src, "category": cat}]

# ----------------------- reason & client DB -----------------------
def _format_name_with_afm(name: str, afm: str, max_len: int = 128) -> str:
    """Format 'Name (AFM)' with character limit, keeping AFM complete and truncating name if needed."""
    if not name or not afm:
        return ""
    afm_part = f"({afm})"
    space_for_name = max_len - len(afm_part) - 1
    if space_for_name > 0:
        truncated_name = name[:space_for_name].rstrip()
        return f"{truncated_name} {afm_part}"
    return afm_part[:max_len]

def _reason_for_rec_enhanced(rec: Dict[str, Any], is_receipt: bool, client_names: Optional[Dict[str, str]] = None) -> str:
    # Prefer explicit fields directly from the invoice JSON
    name_explicit = (rec.get("Name_issuer") or rec.get("issuerName") or rec.get("issuer_name") or
                     rec.get("Name") or rec.get("name") or "")
    afm_explicit  = (rec.get("AFM_issuer") or rec.get("AFM") or "")
    aa            = (rec.get("AA") or rec.get("aa") or rec.get("mark") or "")

    nm = str(name_explicit).strip()
    afm = _norm_afm(afm_explicit)

    if is_receipt:
        # Receipts: "Name_issuer (AFM_issuer)" with 128 char limit
        if nm and afm:
            return _format_name_with_afm(nm, afm, max_len=128)
        if nm:
            return nm[:128]
        if afm:
            return f"({afm})"[:128]
        return (f"Απόδειξη {aa}").strip() or "—"

    # Invoices: try explicit name; else client_db name by AFM; else fallbacks
    if not nm and client_names and afm:
        nm = client_names.get(afm, "").strip()
    if nm:
        return nm
    return (rec.get("ΑΙΤΙΟΛΟΓΙΑ") or rec.get("reason") or f"Τιμολόγιο {aa}").strip() or "—"

# ---- robust CSV/XLS/XLSX reader ----
def _find_header_row(df: pd.DataFrame, scan_rows: int = 30) -> int:
    best = 0
    best_nonempty = -1
    for i in range(min(scan_rows, len(df))):
        nonempty = sum(1 for c in df.iloc[i].tolist() if str(c).strip() and str(c).strip().lower() != "nan")
        if nonempty > best_nonempty:
            best_nonempty = nonempty; best = i
    return best

def _read_first_sheet_any(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        # try ; then ,
        try:
            df = pd.read_csv(path, dtype=str, sep=";")
        except Exception:
            df = pd.read_csv(path, dtype=str)
        df = df.dropna(how="all").dropna(axis=1, how="all")
        for c in list(df.columns):
            df.rename(columns={c: str(c).strip()}, inplace=True)
        return df
    # excel
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        xl = pd.ExcelFile(path)  # engine auto
    sheet = xl.sheet_names[0]
    df_raw = xl.parse(sheet, header=None, dtype=str)
    hdr = _find_header_row(df_raw)
    headers = [str(x).strip() for x in df_raw.iloc[hdr].tolist()]
    df = xl.parse(sheet, header=hdr, dtype=str)
    df.columns = [str(c).strip() for c in headers]
    df = df.dropna(how="all").dropna(axis=1, how="all")
    return df

def _pick_col(cols: List[str], tokens: List[str]) -> Optional[str]:
    lc_map = {str(c).strip().lower(): c for c in cols}
    for lc, orig in lc_map.items():
        for t in tokens:
            if t in lc:
                return orig
    return None

def _load_client_map(path: str) -> Dict[str, Any]:
    df = _read_first_sheet_any(path)
    col_afm  = _pick_col(list(df.columns), ["αφμ", "afm", "vat"])
    col_id   = _pick_col(list(df.columns), ["συναλλ", "κωδ", "cust", "id", "code"])
    col_name = _pick_col(list(df.columns), ["επωνυ", "name"])
    if not col_afm or not col_id:
        raise ValueError(f"client_db: λείπουν στήλες AFM/ID. Columns={list(df.columns)}")
    by_afm: Dict[str, int] = {}
    ids_in_db: set[int] = set()
    names: Dict[str, str] = {}
    for _, r in df.iterrows():
        afm_str = _norm_afm(r.get(col_afm, ""))
        if not afm_str:
            continue
        raw_id = str(r.get(col_id, "")).strip()
        try:
            cid = int(float(raw_id))
        except Exception:
            continue
        by_afm[afm_str] = cid
        ids_in_db.add(cid)
        if col_name:
            nm = str(r.get(col_name, "") or "").strip()
            if nm:
                names[afm_str] = nm
    return {"by_afm": by_afm, "ids": ids_in_db, "names": names, "columns": list(df.columns)}

# ----------------------- paths (robust client_db discovery) -----------------------
def _discover_client_db_in_data_dir(data_dir: str = "data", vat: Optional[str] = None) -> Optional[str]:
    """Find any plausible client db file in /data, even if named differently."""
    if not os.path.isdir(data_dir):
        return None
    candidates = []
    for fn in os.listdir(data_dir):
        lower = fn.lower()
        if lower.endswith((".xlsx", ".xls", ".csv")) and ("client" in lower or "συναλλ" in lower or "customers" in lower):
            full = os.path.join(data_dir, fn)
            size = os.path.getsize(full)
            mtime = os.path.getmtime(full)
            score = 0
            if "client_db" in lower: score += 50
            if "client" in lower: score += 20
            if vat and str(vat) in lower: score += 15
            if lower.endswith(".xlsx"): score += 10
            if lower.endswith(".xls"): score += 8
            if lower.endswith(".csv"): score += 5
            score += min(size // 1024, 100)  # larger sheet slightly preferred
            candidates.append((score, mtime, full))
    if not candidates:
        return None
    # pick by best score then most recent
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return candidates[0][2]

# ----------------------- paths (robust client_db discovery) -----------------------
def resolve_paths_for_vat(
    vat: str,
    invoices_json: Optional[str] = None,
    client_db: Optional[str] = None,
    out_xlsx: Optional[str] = None,
    base_invoices_dir: str = "data/epsilon",
    base_exports_dir: str = "exports",
) -> Dict[str, str]:
    vat = str(vat).strip()

    # Το group base dir είναι ο "γονέας" του base_invoices_dir (π.χ. .../data/<group>)
    base_dir = os.path.abspath(os.path.dirname(base_invoices_dir)) if base_invoices_dir else "data"

    # 1) invoices path – δώσε προτεραιότητα στον φάκελο epsilon του group
    inv_candidates = [
        invoices_json,
        os.path.join(base_invoices_dir, f"{vat}_epsilon_invoices.json"),
        os.path.join(base_invoices_dir, "epsilon_invoices.json"),
        # extra candidates just in case
        os.path.join(base_dir, "epsilon", f"{vat}_epsilon_invoices.json"),
        os.path.join(base_dir, "epsilon", "epsilon_invoices.json"),
    ]
    invoices_path = next((p for p in inv_candidates if p and os.path.exists(p)),
                         os.path.join(base_invoices_dir, f"{vat}_epsilon_invoices.json"))

    # 2) client_db – ψάξε ΠΡΩΤΑ στο group base dir, μετά (fallback) στο data/
    explicit = client_db if (client_db and os.path.exists(client_db)) else None
    conventional = None
    for p in [
        os.path.join(base_dir, f"client_db_{vat}.xlsx"),
        os.path.join(base_dir, f"{vat}_client_db.xlsx"),
        os.path.join(base_dir, "client_db.xlsx"),
        os.path.join(base_dir, f"client_db_{vat}.xls"),
        os.path.join(base_dir, f"{vat}_client_db.xls"),
        os.path.join(base_dir, "client_db.xls"),
        os.path.join(base_dir, "client_db.csv"),
        # fallbacks στο παλιό "data/"
        os.path.join("data", f"client_db_{vat}.xlsx"),
        os.path.join("data", f"{vat}_client_db.xlsx"),
        os.path.join("data", "client_db.xlsx"),
        os.path.join("data", f"client_db_{vat}.xls"),
        os.path.join("data", f"{vat}_client_db.xls"),
        os.path.join("data", "client_db.xls"),
        os.path.join("data", "client_db.csv"),
    ]:
        if os.path.exists(p):
            conventional = p
            break

    discovered = (_discover_client_db_in_data_dir(base_dir, vat=vat)
                  or _discover_client_db_in_data_dir("data", vat=vat))
    client_db_path = explicit or conventional or discovered

    # 3) out path – άσε να παραμετροποιείται (στο app θα περάσουμε group exports)
    out_path = out_xlsx or os.path.join(base_exports_dir, f"{vat}_EPSILON_BRIDGE_KINHSEIS.xlsx")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

    return {"invoices": invoices_path, "client_db": client_db_path, "out": out_path}

# ----------------------- per-line account resolution -----------------------
def _account_detail_for_line(
    settings: Dict[str, Any],
    category: str,
    is_receipt: bool,
    vat_rate: Optional[int],
    analysis_enabled: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    canon = _canon_category(category)
    tried: List[str] = []
    chosen = ""
    used_key = ""

    forced_rate = 0 if (canon == "εγγυοδοσια" or (is_receipt and not analysis_enabled)) else None
    target_rate = int(forced_rate) if forced_rate is not None else (int(vat_rate) if vat_rate is not None else None)

    if target_rate is None:
        dbg = {
            "category": canon,
            "vat_in": vat_rate,
            "analysis_enabled": bool(analysis_enabled),
            "forced_zero": bool(forced_rate is not None),
            "used_key": "",
            "tried_keys": [],
            "chosen": "",
        }
        return "", dbg

    setts = _settings_norm(settings)
    for key in _account_key_candidates(canon, target_rate):
        tried.append(key)
        val = setts.get(key, "")
        if isinstance(val, str) and val.strip():
            chosen, _ = _split_account_value(val)
            used_key = key
            break

    dbg = {
        "category": canon,
        "vat_in": vat_rate,
        "analysis_enabled": bool(analysis_enabled),
        "forced_zero": bool(forced_rate is not None),
        "used_key": used_key,
        "tried_keys": tried,
        "chosen": chosen,
    }
    return chosen, dbg



# ----------------------- fiscal-year helpers -----------------------
def _read_active_fiscal_year(base_invoices_dir: str) -> Optional[int]:
    """Try to read the active fiscal year from a sibling fiscal_meta.json next to base_invoices_dir.
    Falls back to None if not found or invalid.
    """
    try:
        base_dir = os.path.dirname(base_invoices_dir) if base_invoices_dir else "data"
        # If base_invoices_dir = "data/epsilon", parent is "data"
        p = os.path.join(base_dir, "fiscal_meta.json")
        if not os.path.exists(p):
            # also try plain "data/fiscal_meta.json"
            p2 = os.path.join("data", "fiscal_meta.json")
            p = p2 if os.path.exists(p2) else p
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        fy = data.get("fiscal_year")
        if fy is None:
            return None
        try:
            return int(fy)
        except Exception:
            return None
    except Exception:
        return None
# ----------------------- preview/export -----------------------
def characts_from_lines(rec: Dict[str, Any]) -> str:
    cats: List[str] = []
    for ln in (rec.get("lines") or []):
        c = (ln.get("category") or "").strip()
        if c:
            cats.append(_canon_category(c))
    if not cats and rec.get("category"):
        cats = [_canon_category(rec["category"])]
    return ", ".join(sorted({c for c in cats if c}))

def build_preview_rows_for_ui(
    vat: str,
    credentials_json: str = "data/credentials.json",
    cred_settings_json: str = "data/credentials_settings.json",
    invoices_json: Optional[str] = None,
    client_db: Optional[str] = None,
    base_invoices_dir: str = "data/epsilon",
    fiscal_year: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:

    paths = resolve_paths_for_vat(vat, invoices_json, client_db, None, base_invoices_dir)
    issues: List[Dict[str, Any]] = []

    try:
        invoices = load_epsilon_invoices(paths["invoices"])

    except Exception as e:
        return [], [{"code":"invoices_read_error","modal":True,"message":f"Σφάλμα invoices: {e}"}], False

    
    # --- Fiscal year filter (bridge only for the selected fiscal year) ---
    fy = fiscal_year if (locals().get("fiscal_year", None) is not None) else _read_active_fiscal_year(base_invoices_dir)
    if fy is not None:
        _filtered = []
        for _rec in invoices:
            _dt = _to_date(_rec.get("issueDate") or _rec.get("ΗΜΕΡΟΜΗΝΙΑ"))
            if _dt is None or _dt.year != int(fy):
                try:
                    _aa = _rec.get("AA") or _rec.get("aa") or ""
                    _mark = _rec.get("MARK") or _rec.get("mark") or ""
                    
                except Exception:
                    pass
                continue
            _filtered.append(_rec)
        invoices = _filtered

    credentials = _safe_json_read(credentials_json, default=[])
    settings_all = _safe_json_read(cred_settings_json, default={})

    cred_list = credentials if isinstance(credentials, list) else [credentials]
    active = next((c for c in cred_list if str(c.get("vat")) == str(vat)), (cred_list[0] if cred_list else {}))
    settings_all = _merge_custom_accounts(settings_all, active)
    apod_type = (active or {}).get("apodeixakia_type", "")
    apod_supplier_id = _safe_int((active or {}).get("apodeixakia_supplier",""))
    other_expenses_flag = 1 if bool((active or {}).get("apodeixakia_other_expenses")) else 0

    # client map
    client_map = {"by_afm": {}, "by_id": set(), "names": {}, "cols": []}
    if paths["client_db"] and os.path.exists(paths["client_db"]):
        try:
            cm = _load_client_map(paths["client_db"])
            client_map["by_afm"] = cm.get("by_afm", {})
            client_map["by_id"]  = cm.get("ids", set())
            client_map["names"]  = cm.get("names", {})
            client_map["cols"]   = cm.get("columns", [])
        except Exception as e:
            issues.append({"code":"client_db_read_error","modal":True,"message":f"client_db: {e}"})
    else:
        issues.append({"code":"client_db_missing","modal":True,"message":"Δεν βρέθηκε client_db για αντιστοίχιση CUSTID (κοίτα τον φάκελο data/)."})

    # Tracking για προσωρινούς/νέους συναλλασσόμενους (όπως Γ κατηγορία)
    new_suppliers: Dict[str, Dict[str, Any]] = {}
    next_custid = (max(client_map["by_id"]) + 1) if (client_map["by_id"] or set()) else 1

    rows: List[Dict[str, Any]] = []

    for rec in invoices:
        mark = rec.get("MARK") or rec.get("mark") or ""
        aa = rec.get("AA") or rec.get("aa") or ""
        date = _ddmmyyyy(rec.get("issueDate") or rec.get("ΗΜΕΡΟΜΗΝΙΑ"))
        afm_raw = rec.get("AFM_issuer") or rec.get("AFM") or ""
        afm_norm = _norm_afm(afm_raw)
        is_receipt = _is_receipt(rec)
        doc_type = rec.get("type") or ""
        receipt_other_flag = 1 if (is_receipt and other_expenses_flag) else 0

        issuer_name = (
            rec.get("Name_issuer")
            or rec.get("issuerName")
            or rec.get("issuer_name")
            or rec.get("Name")
            or rec.get("name")
            or client_map["names"].get(afm_norm, "")
        )
        reason = _reason_for_rec_enhanced(rec, is_receipt, client_map.get("names"))

        pairs = _parse_lines(rec)
        tot_net = sum(p["net"] for p in pairs)
        tot_vat = sum(p["vat"] for p in pairs)
        tot_gross = tot_net + tot_vat

        # CUSTID
        custid_val: Optional[int] = None
        if is_receipt and apod_type == "supplier":
            if apod_supplier_id is not None and apod_supplier_id in (client_map["by_id"] or set()):
                custid_val = apod_supplier_id
            else:
                # Do not hard-block export when supplier setting points to missing CUSTID.
                # Fallback to issuer AFM mapping (or auto-create supplier) and keep a non-fatal issue.
                custid_val = client_map["by_afm"].get(afm_norm)
                if custid_val is None:
                    if afm_norm in new_suppliers:
                        custid_val = new_suppliers[afm_norm]["custid"]
                    else:
                        custid_val = next_custid
                        counterpart_name_raw = str(
                            rec.get("Name_issuer")
                            or rec.get("issuerName")
                            or rec.get("issuer_name")
                            or rec.get("Name")
                            or rec.get("name")
                            or f"Συναλλασσόμενος {afm_norm}"
                        ).strip()
                        # Apply same formatting as receipts: "Name (AFM)" with 128 char limit
                        if counterpart_name_raw and not counterpart_name_raw.startswith("Συναλλασσόμενος"):
                            counterpart_name = _format_name_with_afm(counterpart_name_raw, afm_norm, max_len=128)
                        else:
                            counterpart_name = counterpart_name_raw
                        new_suppliers[afm_norm] = {
                            "custid": custid_val,
                            "name": counterpart_name,
                        }
                        next_custid += 1
                        issues.append({
                            "code": "auto_created_supplier",
                            "modal": False,
                            "message": f"Δημιουργήθηκε αυτόματα νέος συναλλασσόμενος: CUSTID={custid_val}, AFM={afm_norm}, NAME={counterpart_name}",
                        })

                issues.append({
                    "code":"apodeixakia_supplier_not_in_client_db_fallback",
                    "modal":False,
                    "message": f"Απόδειξη AA={aa}: apodeixakia_supplier={apod_supplier_id} δεν υπάρχει στο client_db. Χρησιμοποιήθηκε fallback CUSTID={custid_val}."
                })
        else:
            custid_val = client_map["by_afm"].get(afm_norm)
            if custid_val is None:
                if afm_norm in new_suppliers:
                    custid_val = new_suppliers[afm_norm]["custid"]
                else:
                    custid_val = next_custid
                    counterpart_name_raw = str(
                        rec.get("Name_issuer")
                        or rec.get("issuerName")
                        or rec.get("issuer_name")
                        or rec.get("Name")
                        or rec.get("name")
                        or f"Συναλλασσόμενος {afm_norm}"
                    ).strip()
                    # Apply same formatting as receipts: "Name (AFM)" with 128 char limit
                    if counterpart_name_raw and not counterpart_name_raw.startswith("Συναλλασσόμενος"):
                        counterpart_name = _format_name_with_afm(counterpart_name_raw, afm_norm, max_len=128)
                    else:
                        counterpart_name = counterpart_name_raw
                    new_suppliers[afm_norm] = {
                        "custid": custid_val,
                        "name": counterpart_name,
                    }
                    next_custid += 1
                    issues.append({
                        "code": "auto_created_supplier",
                        "modal": False,
                        "message": f"Δημιουργήθηκε αυτόματα νέος συναλλασσόμενος: CUSTID={custid_val}, AFM={afm_norm}, NAME={counterpart_name}",
                    })

        # Supplier header account
        lcode_p = _account_header_P(settings_all, is_receipt)
        if not lcode_p:
            issues.append({"code":"missing_header_account","modal":True,
                           "message": f"AA={aa}: Δεν έχει οριστεί account_supplier_{'retail' if is_receipt else 'wholesale'}."})

        lines_out: List[Dict[str, Any]] = []
        lcodes_summary: List[str] = []

        analysis_enabled = _receipt_analysis_enabled(rec)

        for ln in (_parse_lines(rec) or []):
            vr = ln.get("vat_rate")
            src = ln.get("vat_src")
            cat = ln.get("category") or rec.get("category") or ""
            acc, dbg = _account_detail_for_line(settings_all, cat, is_receipt, vr, analysis_enabled=analysis_enabled)

            if (not cat) and (not is_receipt):
                issues.append({"code":"missing_category_line","modal":True,"message":f"AA={aa} — Γραμμή χωρίς κατηγορία. Συμπλήρωσε χαρακτηρισμό."})
            if (vr is None) and (not is_receipt or _canon_category(cat) != "αποδειξακια"):
                issues.append({"code":"missing_vat_rate_line","modal":True,"message":f"AA={aa} — Δεν προέκυψε ποσοστό ΦΠΑ για γραμμή."})
            if not acc:
                expected_rate = 0 if (_canon_category(cat) == 'εγγυοδοσια' or (is_receipt and not analysis_enabled)) else (vr if vr is not None else '?')
                exp = f"account_{_canon_category(cat)}_fpa_kat_{expected_rate}%"
                issues.append({"code":"unresolved_account_line","modal":True,"message":f"AA={aa} — Δεν βρέθηκε λογαριασμός για '{_canon_category(cat)}' ({vr}%). Ρύθμισε {exp} στα settings."})

            lcodes_summary.append(acc or "")
            net_val = float(ln.get("net", 0.0))
            vat_val = float(ln.get("vat", 0.0))
            lines_out.append({
                "category": _canon_category(cat),
                "vat_rate_in": vr,
                "vat_rate_source": src,
                "lcode_detail": acc,
                "debug": dbg,
                "net": _round2(net_val),
                "vat": _round2(vat_val),
                "gross": _round2(net_val + vat_val),
            })

        characts = characts_from_lines(rec)

        rows.append({
            "MARK": str(mark),
            "AA": str(aa),
            "SERIES": str(rec.get("series") or rec.get("SERIES") or ""),
            "DATE": date,
            "AFM_ISSUER": afm_norm or str(afm_raw),
            "ISSUER_NAME": issuer_name,
            "CUSTID": custid_val,
            "NET": _round2(tot_net),
            "VAT": _round2(tot_vat),
            "GROSS": _round2(tot_gross),
            "DOCTYPE": doc_type,
            "REASON": reason,
            "CHARACTS": characts,
            "LINES": lines_out,
            "LCODE_DETAIL_SUMMARY": ", ".join(sorted({(c if isinstance(c, str) else str(c)) for c in lcodes_summary if c})),
            "LCODE": (lcode_p or ""),
            "OTHEREXPEND": receipt_other_flag,
        })

    ok = (len(rows) > 0)
    return rows, issues, ok

def build_preview_strict_multiclient(
    vat: str,
    credentials_json: str,
    cred_settings_json: str,
    invoices_json: Optional[str] = None,
    client_db: Optional[str] = None,
    base_invoices_dir: str = "data/epsilon",
    fiscal_year: Optional[int] = None,
) -> Dict[str, Any]:
    rows, issues, ok = build_preview_rows_for_ui(
        vat=vat,
        credentials_json=credentials_json,
        cred_settings_json=cred_settings_json,
        invoices_json=invoices_json,
        client_db=client_db,
        base_invoices_dir=base_invoices_dir,
        fiscal_year=fiscal_year
    )
    paths = resolve_paths_for_vat(vat, invoices_json, client_db, None, base_invoices_dir)
    return {"ok": ok and not issues, "rows": rows, "issues": issues, "paths": paths}

def load_epsilon_invoices(path: str) -> List[Dict[str, Any]]:
    data = json.load(open(path, "r", encoding="utf-8"))
    if isinstance(data, list):
        return data
    for key in ("invoices", "records", "rows", "data", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    return [data]

def _compose_invoice_value(rec: Dict[str, Any]) -> str:
    """Return the bridge 'INVOICE' column value with optional series prefix."""
    try:
        series = str(rec.get("SERIES") or rec.get("series") or "").strip()
    except Exception:
        series = ""
    try:
        aa_val = str(rec.get("AA") or rec.get("aa") or rec.get("mark") or "").strip()
    except Exception:
        aa_val = ""
    if series and aa_val:
        return f"{series} {aa_val}".strip()
    return series or aa_val

def export_multiclient_strict(
    vat: str,
    credentials_json: str,
    cred_settings_json: str,
    invoices_json: Optional[str] = None,
    client_db: Optional[str] = None,
    out_xlsx: Optional[str] = None,
    base_invoices_dir: str = "data/epsilon",
    base_exports_dir: str = "exports",
    fiscal_year: Optional[int] = None):
    """
    ΜΟΝΟ export: κρατάει ΚΙΝΗΣΕΙΣ όπως είναι και χτίζει ΣΥΝΑΛΛΑΣΣΟΜΕΝΟΥΣ,
    χωρίς να αλλάξει τίποτα στη λογική εύρεσης λογαριασμών/κατηγοριών/ΦΠΑ.
    """
    preview = build_preview_strict_multiclient(
        vat=vat,
        credentials_json=credentials_json,
        cred_settings_json=cred_settings_json,
        invoices_json=invoices_json,
        client_db=client_db,
        base_invoices_dir=base_invoices_dir,
        fiscal_year=fiscal_year)
    nonfatal_codes = {"filtered_out_by_year", "auto_created_supplier", "apodeixakia_supplier_not_in_client_db_fallback"}
    fatals = [i for i in preview["issues"] if str(i.get("code","")) not in nonfatal_codes]
    if fatals:
        return False, "", fatals
    # capture non-fatal issues (e.g., filtered_out_by_year); we will return them alongside success
    nonfatal_issues = [i for i in preview["issues"] if str(i.get("code","")) in nonfatal_codes]

    # Δημιουργία custom out_xlsx path με prefix "B_CATEGORY_" για διαχωρισμό
    if not out_xlsx:
        out_xlsx = os.path.join(base_exports_dir, f"{vat}_B_CATEGORY_EPSILON_BRIDGE_KINHSEIS.xlsx")

    paths = resolve_paths_for_vat(
        vat, invoices_json, client_db, out_xlsx, base_invoices_dir, base_exports_dir
    )
    rows = preview["rows"]           # rows από τον builder (ΔΕΝ τα αλλάζουμε)
    issues: List[Dict[str, Any]] = []  # δεν προσθέτουμε νέα issues εδώ
    try:
        issues.extend(nonfatal_issues)
    except NameError:
        pass


    # ---------- Φτιάξε ΚΙΝΗΣΕΙΣ όπως ΕΙΝΑΙ (δεν πειράζουμε mapping/λογικές) ----------
    flat: List[Dict[str, Any]] = []
    artid = 1
    
    # Ελέγχουμε το book_category για να αποφασίσουμε το MTYPE
    is_b_category = False
    try:
        credentials = _safe_json_read(credentials_json, default=[])
        cred_list = credentials if isinstance(credentials, list) else [credentials]
        active = next((c for c in cred_list if str(c.get("vat")) == str(vat)), (cred_list[0] if cred_list else {}))
        book_category = str(active.get("book_category") or "Β").strip().upper()
        is_b_category = (book_category == "Β")
    except Exception:
        is_b_category = False
    
    for rec in rows:
        is_receipt = any(
            k in str(rec.get("DOCTYPE", "")).lower()
            for k in ["receipt", "αποδείξ", "αποδειξ", "λιαν"]
        )
        msign = 1
        sum_net = float(rec.get("NET", 0.0))
        if sum_net < 0:
            msign = -1
        for ln in rec.get("LINES", []):
            # Για Β κατηγορία, MTYPE = 1 πάντα. Αλλιώς, χρησιμοποιούμε την παλιά λογική
            if is_b_category:
                mtype_val = 1
            else:
                mtype_val = 1 if is_receipt or ("αγορ" in (ln.get("category", "") or "") or "δαπ" in (ln.get("category", "") or "")) else 0
            
            flat.append({
                "ARTID": artid,
                "MTYPE": mtype_val,
                "ISKEPYO": 1,
                "ISAGRYP": 0,
                "CUSTID": rec.get("CUSTID"),
                "MDATE": _to_date(rec.get("DATE")),
                "REASON": rec.get("REASON"),
                "INVOICE": _compose_invoice_value(rec),
                "SUMKEPYOYP": float(abs(sum_net)),
                "LCODE_DETAIL": ln.get("lcode_detail") or "",
                "ISAGRYP_DETAIL": 0,
                "KEPYOPARTY_DETAIL": float(abs(ln.get("net", 0.0))),
                "NETAMT_DETAIL": float(abs(ln.get("net", 0.0))),
                "VATAMT_DETAIL": float(abs(ln.get("vat", 0.0))),
                "MSIGN": msign,
                "LCODE": rec.get("LCODE") or "",
                "OTHEREXPEND": int(rec.get("OTHEREXPEND", 0) or 0),
            })
        artid += 1

    df_moves = pd.DataFrame(flat, columns=[
        "ARTID","MTYPE","ISKEPYO","ISAGRYP","CUSTID","MDATE","REASON","INVOICE","SUMKEPYOYP",
        "LCODE_DETAIL","ISAGRYP_DETAIL","KEPYOPARTY_DETAIL","NETAMT_DETAIL","VATAMT_DETAIL","MSIGN","LCODE","OTHEREXPEND"
    ])

    # ---------- Διαβάσε ρυθμίσεις για supplier mode (χωρίς να αλλάξεις τίποτα άλλο) ----------
    try:
        credentials = _safe_json_read(credentials_json, default=[])
    except Exception:
        credentials = []
    cred_list = credentials if isinstance(credentials, list) else [credentials]
    active = next((c for c in cred_list if str(c.get("vat")) == str(vat)), (cred_list[0] if cred_list else {}))
    apod_type = (active or {}).get("apodeixakia_type", "")
    apod_supplier_id = _safe_int((active or {}).get("apodeixakia_supplier", ""))

    # ---------- Χτίσε ΣΥΝΑΛΛΑΣΣΟΜΕΝΟΥΣ με ΜΟΝΗ προσαρμογή για supplier mode ----------
    # 1) used CUSTIDs από το df_moves
    import math as _math
    used_ids_unique: List[Any] = []
    if not df_moves.empty and "CUSTID" in df_moves.columns:
        for x in df_moves["CUSTID"].tolist():
            if x is None:
                continue
            if isinstance(x, float):
                try:
                    if _math.isnan(x):
                        continue
                except Exception:
                    pass
            try:
                fx = float(x)
                x = int(fx) if fx.is_integer() else x
            except Exception:
                pass
            if x not in used_ids_unique:
                used_ids_unique.append(x)

    # 2) Χτίσε mapping CUSTID -> (AFM, NAME) από τα rows (χωρίς αλλαγές σε λογικές)
    partners: Dict[Any, Tuple[str, str]] = {}
    for rec in rows:
        cid = rec.get("CUSTID")
        if cid in (None, ""):
            continue
        afm = _norm_afm(rec.get("AFM_ISSUER") or rec.get("AFM") or "")
        nm  = str(rec.get("ISSUER_NAME") or rec.get("Name") or "").strip()
        if cid not in partners:
            partners[cid] = (afm, nm)

    # 3) Αν είναι supplier mode και ο supplier id χρησιμοποιήθηκε, ΕΠΙΒΑΛΕ default “000000000 / ΠΡΟΜΗΘΕΥΤΕΣ ΔΑΠΑΝΩΝ”
    if str(apod_type).lower() == "supplier" and apod_supplier_id not in (None, ""):
        if apod_supplier_id in used_ids_unique:
            partners[apod_supplier_id] = ("000000000", "ΠΡΟΜΗΘΕΥΤΕΣ ΔΑΠΑΝΩΝ")

    # 4) Κράτα ΜΟΝΟ όσους χρησιμοποιήθηκαν πράγματι στις κινήσεις (με τη σωστή σειρά)
    partners_rows: List[Dict[str, Any]] = []
    for cid in used_ids_unique:
        afm, nm = partners.get(cid, ("", ""))
        partners_rows.append({"Α/Α": cid, "ΑΦΜ": afm, "ΕΠΩΝΥΜΙΑ": nm})

    df_partners = pd.DataFrame(partners_rows, columns=["Α/Α","ΑΦΜ","ΕΠΩΝΥΜΙΑ"]).drop_duplicates(subset=["Α/Α"])
    try:
        df_partners["_k"] = df_partners["Α/Α"].apply(lambda x: int(x) if str(x).isdigit() else x)
        df_partners = df_partners.sort_values(by="_k", kind="mergesort").drop(columns=["_k"])
    except Exception:
        pass

    # ---------- Γράψε Excel: ΚΙΝΗΣΕΙΣ + ΣΥΝΑΛΛΑΣΣΟΜΕΝΟΙ ----------
    with pd.ExcelWriter(paths["out"], engine="xlsxwriter", datetime_format="dd/mm/yyyy") as writer:
        # ΚΙΝΗΣΕΙΣ (ίδιο format όπως πριν)
        df_moves.to_excel(writer, index=False, sheet_name="ΚΙΝΗΣΕΙΣ")
        wb, ws = writer.book, writer.sheets["ΚΙΝΗΣΕΙΣ"]
        fmt_num  = wb.add_format({"num_format": "0.00"})
        fmt_int  = wb.add_format({"num_format": "0"})
        fmt_date = wb.add_format({"num_format": "dd/mm/yyyy"})
        idx = {n: i for i, n in enumerate(df_moves.columns)}
        for n in ["ARTID","MTYPE","ISKEPYO","ISAGRYP","ISAGRYP_DETAIL","MSIGN","CUSTID","OTHEREXPEND"]:
            if n in idx:
                ws.set_column(idx[n], idx[n], 10, fmt_int)
        for n in ["SUMKEPYOYP","KEPYOPARTY_DETAIL","NETAMT_DETAIL","VATAMT_DETAIL"]:
            if n in idx:
                ws.set_column(idx[n], idx[n], 14, fmt_num)
        if "MDATE" in idx:
            ws.set_column(idx["MDATE"], idx["MDATE"], 12, fmt_date)
        for n, w in [("REASON", 40), ("INVOICE", 18), ("LCODE_DETAIL", 16), ("LCODE", 16)]:
            if n in idx:
                ws.set_column(idx[n], idx[n], w)

        # ΣΥΝΑΛΛΑΣΣΟΜΕΝΟΙ
        if df_partners.empty:
            df_partners = pd.DataFrame(columns=["Α/Α","ΑΦΜ","ΕΠΩΝΥΜΙΑ"])
        df_partners.to_excel(writer, index=False, sheet_name="ΣΥΝΑΛΛΑΣΣΟΜΕΝΟΙ")
        ws2 = writer.sheets["ΣΥΝΑΛΛΑΣΣΟΜΕΝΟΙ"]
        try:
            ws2.set_column(0, 0, 8,  fmt_int)  # Α/Α
            ws2.set_column(1, 1, 14)           # ΑΦΜ
            ws2.set_column(2, 2, 40)           # ΕΠΩΝΥΜΙΑ
        except Exception:
            pass

    return True, paths["out"], issues


@monitor_resources('run_and_report_dynamic')
def run_and_report_dynamic(
    vat: str,
    credentials_json: str = "data/credentials.json",
    cred_settings_json: str = "data/credentials_settings.json",
    invoices_json: Optional[str] = None,
    client_db: Optional[str] = None,
    out_xlsx: Optional[str] = None,
    base_invoices_dir: str = "data/epsilon",
    base_exports_dir: str = "exports",
):
    ok, path, issues = export_multiclient_strict(
        vat=vat,
        credentials_json=credentials_json,
        cred_settings_json=cred_settings_json,
        invoices_json=invoices_json,
        client_db=client_db,
        out_xlsx=out_xlsx,
        base_invoices_dir=base_invoices_dir,
        base_exports_dir=base_exports_dir,
    )
    return {"ok": ok, "path": path, "issues": issues}

# ----------------------- CLI -----------------------
def _cli():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--vat", required=True)
    ap.add_argument("--invoices", default=None)
    ap.add_argument("--clientdb", default=None)
    ap.add_argument("--credentials", default="data/credentials.json")
    ap.add_argument("--settings", default="data/credentials_settings.json")
    ap.add_argument("--out", default=None)
    ap.add_argument("--invoices_dir", default="data/epsilon")
    ap.add_argument("--exports_dir", default="exports")
    args = ap.parse_args()

    ok, path, issues = export_multiclient_strict(
        vat=args.vat,
        credentials_json=args.credentials,
        cred_settings_json=args.settings,
        invoices_json=args.invoices,
        client_db=args.clientdb,
        out_xlsx=args.out,
        base_invoices_dir=args.invoices_dir,
        base_exports_dir=args.exports_dir,
    )
    if not ok:
        print("ERROR: Validation failed. Issues:")
        for i in issues:
            print(" -", i.get("message"))
    else:
        print("Wrote:", path)

if __name__ == "__main__":
    _cli()
