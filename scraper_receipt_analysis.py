#!/usr/bin/env python3
# scraper.py - unified scrapers producing same output schema for multiple sources
import re
import json
import base64
import html as html_lib
import requests
import urllib.request
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin, urlparse, parse_qs, unquote, quote, urlencode

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "el-GR,el;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://mydata.wedoconnect.com/",
    "DNT": "1",
}

MARK_RE = re.compile(r"\b\d{15}\b")
VAT_RE = re.compile(r"\b\d{9}\b")
AMOUNT_RE = re.compile(r"(-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)")
DATE_PATTERNS = [r"(\d{4}-\d{2}-\d{2})", r"(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4})", r"(\d{4}\/\d{2}\/\d{2})"]

def _normalize_url(url: str) -> str:
    """
    Διορθώνει συνηθισμένα συντακτικά λάθη σε URLs, π.χ.:
    - https:/example.com → https://example.com
    - http:/example.com → http://example.com
    """
    if not url:
        return url
    url = str(url).strip()
    # Διόρθωση λάθους protocol: https:/ → https://
    url = re.sub(r'^(https?):/([^/])', r'\1://\2', url)
    return url

def _fetch_url_text(url, headers=None, timeout=15, debug=False):
    """
    Best-effort fetch with retries for unstable endpoints (e.g. AADE pages).
    Returns decoded text or raises the last exception.
    """
    headers = headers or HEADERS
    last_err = None

    # 1) direct requests attempts
    for _ in range(2):
        try:
            r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as e:
            last_err = e

    # 2) session-based attempt with explicit connection close
    try:
        sess = requests.Session()
        req_headers = dict(headers)
        req_headers["Connection"] = "close"
        r = sess.get(url, headers=req_headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        last_err = e

    # 3) urllib fallback (different HTTP stack)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
    except Exception as e:
        last_err = e

    if debug and last_err is not None:
        print("fetch failed after retries:", last_err)
    raise last_err if last_err is not None else RuntimeError("Unknown fetch error")

# ---------- helpers ----------

def _clean_amount_to_comma(raw):
    """
    Καθαρίζει ποσά: αφαιρεί € και επιστρέφει δεκαδικό με κόμμα.
    Π.χ. "€ 15.17" -> "15,17", "1.234,56" -> "1234,56"
    """
    if raw is None:
        return None
    s = str(raw).strip()
    s = s.replace("€", "").replace("EUR", "").strip()
    # remove non-number except dot/comma and minus
    s = s.replace('\xa0', '').replace(' ', '')
    # handle cases with both separators
    if '.' in s and ',' in s:
        # if last separator is comma treat comma as decimal
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '')
        else:
            s = s.replace(',', '')
            s = s.replace('.', ',')
    else:
        if '.' in s and ',' not in s:
            s = s.replace('.', ',')
    # remove anything but digits, comma, minus
    s = re.sub(r'[^\d,\-]', '', s)
    # if multiple commas keep last as decimal
    if s.count(',') > 1:
        parts = s.split(',')
        decimals = parts[-1]
        integer = ''.join(parts[:-1])
        s = integer + ',' + decimals
    return s or None

def _fmt_date_to_ddmmyyyy(s):
    """
    Επιστρέφει ημερομηνία σε dd/mm/YYYY αν μπορεί να την παρσει.
    Δέχεται ISO, dd/mm/yyyy, yyyy-mm-dd, κτλ.
    """
    if not s:
        return None
    s = str(s).strip()
    # try iso-like
    try:
        # only date part
        dpart = s.split()[0]
        dt = datetime.fromisoformat(dpart)
        return dt.strftime("%d/%m/%Y")
    except Exception:
        pass
    # try several formats
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y"):
        try:
            dpart = s.split()[0]
            dt = datetime.strptime(dpart, fmt)
            return dt.strftime("%d/%m/%Y")
        except Exception:
            continue
    # regex fallback dd/mm/yyyy
    m = re.search(r"(\d{1,2})[\/\-\.\s](\d{1,2})[\/\-\.\s](\d{4})", s)
    if m:
        d, mo, y = m.groups()
        return f"{int(d):02d}/{int(mo):02d}/{int(y)}"
    # yyyy-mm-dd fallback
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m2:
        y, mo, d = m2.groups()
        return f"{int(d):02d}/{int(mo):02d}/{int(y)}"
    return None

def _extract_from_ubl_root(root):
    """
    Best-effort εξαγωγή από XML (UBL-like).
    Επιστρέφει dict με τα επιθυμητά πεδία (όπου βρεθούν).
    """
    out = {
        "issuer_vat": None,
        "issue_date": None,
        "issuer_name": None,
        "progressive_aa": None,
        "doc_type": None,
        "total_amount": None,
        "MARK": None
    }

    def find_text_by_localname(elem, localname):
        for el in elem.iter():
            if el.tag is None:
                continue
            ln = el.tag.split('}')[-1].lower()
            if ln == localname.lower() and el.text and el.text.strip():
                return el.text.strip()
        return None

    # issuer VAT: common locations
    for tagname in ("AccountingSupplierParty", "SupplierParty", "AccountingCustomerParty", "Supplier", "AccountingSupplier"):
        for candidate in root.findall(".//{*}" + tagname):
            v = find_text_by_localname(candidate, "vatNumber") or find_text_by_localname(candidate, "CompanyID") or find_text_by_localname(candidate, "ID")
            if v:
                m = VAT_RE.search(v)
                if m:
                    out["issuer_vat"] = m.group(0)
                    break
        if out["issuer_vat"]:
            break

    # fallback scan full xml text for VAT
    if not out["issuer_vat"]:
        all_text = ET.tostring(root, encoding="utf-8", method="text").decode("utf-8")
        m = VAT_RE.search(all_text)
        if m:
            out["issuer_vat"] = m.group(0)

    # issue date
    for el in root.iter():
        ln = el.tag.split("}")[-1].lower()
        if ln in ("issuedate", "issue_date", "date", "documentdate"):
            if el.text and el.text.strip():
                out["issue_date"] = _fmt_date_to_ddmmyyyy(el.text.strip())
                break

    # issuer name
    for el in root.iter():
        ln = el.tag.split("}")[-1].lower()
        if ln in ("partyname", "name", "companyname", "suppliername"):
            if el.text and el.text.strip():
                out["issuer_name"] = el.text.strip()
                break

    # progressive a/a (look for sequence-like tags)
    for el in root.iter():
        ln = el.tag.split("}")[-1].lower()
        if any(k in ln for k in ("sequential", "sequence", "progres", "saa", "serial", "progress")):
            txt = (el.text or "").strip()
            if txt and re.search(r"\d{2,}", txt):
                out["progressive_aa"] = re.sub(r"\D", "", txt)
                break

    # doc type
    for el in root.iter():
        ln = el.tag.split("}")[-1].lower()
        if ln in ("invoicetypecode", "documenttype", "documenttypecode", "typedocument", "doctype"):
            if el.text and el.text.strip():
                out["doc_type"] = el.text.strip()
                break

    # total amount
    for el in root.iter():
        ln = el.tag.split("}")[-1].lower()
        if ln in ("payableamount", "legalmonetarytotal", "grandtotal", "totalamount", "amount"):
            txt = (el.text or "").strip()
            if txt and re.search(r"[0-9]", txt):
                out["total_amount"] = _clean_amount_to_comma(txt)
                break
    # fallback numeric search
    if not out["total_amount"]:
        root_text = ET.tostring(root, encoding="utf-8", method="text").decode("utf-8")
        m = re.search(r"([0-9]{1,3}(?:[.,][0-9]{3})*[.,][0-9]{1,2})", root_text)
        if m:
            out["total_amount"] = _clean_amount_to_comma(m.group(1))

    # MARK
    mark = find_text_by_localname(root, "mark") or find_text_by_localname(root, "tmark") or find_text_by_localname(root, "markid")
    if mark:
        mm = MARK_RE.search(mark)
        if mm:
            out["MARK"] = mm.group(0)

    return out

def _norm_date_to_ddmmyyyy(s):
    if not s:
        return None
    s = str(s).strip()
    # try iso-ish
    try:
        tok = s.split()[0]
        dt = datetime.fromisoformat(tok)
        return dt.strftime("%d/%m/%Y")
    except Exception:
        pass
    for p in DATE_PATTERNS:
        m = re.search(p, s)
        if m:
            tok = m.group(1)
            tok2 = tok.replace("-", "/")
            parts = tok2.split("/")
            if len(parts) == 3:
                if len(parts[0]) == 4:  # yyyy/mm/dd
                    y, mo, d = parts
                else:
                    d, mo, y = parts
                try:
                    dt = datetime(int(y), int(mo), int(d))
                    return dt.strftime("%d/%m/%Y")
                except Exception:
                    continue
    # final fallback: try parsing common datetime formats
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s.split()[0], fmt.split()[0])
            return dt.strftime("%d/%m/%Y")
        except Exception:
            pass
    return None

def _clean_amount_to_comma(s):
    if s is None:
        return None
    raw = str(s).strip()
    raw = re.sub(r"[^\d\.,\-]", "", raw)
    if raw == "":
        return None
    # both separators present: last marks decimal
    if raw.count(",") and raw.count("."):
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "")
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    if raw.count(",") and not raw.count("."):
        raw = raw.replace(",", ".")
    try:
        val = float(raw)
    except Exception:
        m = AMOUNT_RE.search(str(s))
        if not m:
            return None
        tok = m.group(1).replace(",", ".")
        try:
            val = float(tok)
        except Exception:
            return None
    formatted = f"{val:,.2f}"  # '1,234.56'
    formatted = formatted.replace(",", "TMP").replace(".", ",").replace("TMP", ".")
    # remove leading thousands separator if it starts with '.': keep as-is
    # ensure no currency symbol
    return formatted

def _amount_to_float(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("€", "").replace("EUR", "")
    s = re.sub(r"[^\d\.,\-]", "", s)
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "")
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None

def _float_to_comma(v):
    if v is None:
        return None
    try:
        return _clean_amount_to_comma(f"{float(v):.2f}")
    except Exception:
        return None

def _normalize_vat_rate_key(rate):
    if rate is None:
        return None
    s = str(rate).strip().replace("%", "").replace(",", ".")
    if not s:
        return None
    try:
        num = float(s)
        if num < 0 or num > 100:
            return None
        if num.is_integer():
            return str(int(num))
        return ("%s" % num).rstrip("0").rstrip(".")
    except Exception:
        m = re.search(r"\d{1,2}(?:[\.,]\d+)?", str(rate))
        if not m:
            return None
        return _normalize_vat_rate_key(m.group(0))

def _extract_vat_breakdown_from_xml_root(root):
    if root is None:
        return {}
    rows = {}
    vat_category_to_rate = {
        "1": "24",
        "2": "13",
        "3": "6",
        "4": "17",
        "5": "9",
        "6": "4",
        "7": "0",
        "8": "0",
    }

    def _add_row(rate, net_val=None, vat_val=None, gross_val=None):
        key = _normalize_vat_rate_key(rate)
        if not key:
            return
        row = rows.setdefault(key, {"net": 0.0, "vat": 0.0, "gross": 0.0})
        if net_val is not None:
            row["net"] += net_val
        if vat_val is not None:
            row["vat"] += vat_val
        if gross_val is not None:
            row["gross"] += gross_val

    # UBL TaxSubtotal blocks
    for subtotal in root.findall(".//{*}TaxSubtotal"):
        rate = None
        for el in subtotal.iter():
            ln = _ns_strip(el.tag).lower()
            txt = (el.text or "").strip()
            if not txt:
                continue
            if ln in ("percent", "taxpercent", "vatrate", "rate") and rate is None:
                rate = txt
        net_val = None
        vat_val = None
        gross_val = None
        taxable = subtotal.find(".//{*}TaxableAmount")
        tax = subtotal.find(".//{*}TaxAmount")
        if taxable is not None and (taxable.text or "").strip():
            net_val = _amount_to_float(taxable.text)
        if tax is not None and (tax.text or "").strip():
            vat_val = _amount_to_float(tax.text)
        if net_val is not None and vat_val is not None:
            gross_val = net_val + vat_val
        _add_row(rate, net_val, vat_val, gross_val)

    # myDATA invoiceDetails blocks (vatCategory + netValue + vatAmount)
    for details in root.findall(".//{*}invoiceDetails"):
        rate = None
        vat_category = None
        net_val = None
        vat_val = None
        gross_val = None
        for el in details.iter():
            ln = _ns_strip(el.tag).lower()
            txt = (el.text or "").strip()
            if not txt:
                continue
            if ln in ("vatpercent", "vatrate", "percent", "rate") and rate is None:
                rate = txt
            elif ln == "vatcategory" and vat_category is None:
                vat_category = txt
            elif ln in ("netvalue", "taxableamount", "lineextensionamount") and net_val is None:
                net_val = _amount_to_float(txt)
            elif ln in ("vatamount", "taxamount") and vat_val is None:
                vat_val = _amount_to_float(txt)
            elif ln in ("grossvalue", "linegrossvalue") and gross_val is None:
                gross_val = _amount_to_float(txt)

        if rate is None and vat_category is not None:
            rate = vat_category_to_rate.get(str(vat_category).strip())
        if gross_val is None and net_val is not None and vat_val is not None:
            gross_val = net_val + vat_val

        _add_row(rate, net_val, vat_val, gross_val)

    if rows:
        result = {
            k: {
                "net_amount": _float_to_comma(v.get("net")),
                "vat_amount": _float_to_comma(v.get("vat")),
                "gross_amount": _float_to_comma(v.get("gross")),
            }
            for k, v in rows.items()
        }
        result["__inferred__"] = False
        return result
    return {}


def _extract_vat_breakdown_from_html(soup, html_text=""):
    if soup is None:
        return {}
    rows = {}
    inferred_used = False

    def _guess_rate_from_amounts(net_val, vat_val):
        if net_val is None or vat_val is None or net_val == 0:
            return None
        target = (vat_val / net_val) * 100.0
        candidates = [24.0, 13.0, 6.0, 17.0, 9.0, 4.0, 0.0]
        best = min(candidates, key=lambda c: abs(c - target))
        if abs(best - target) <= 1.0:
            return _normalize_vat_rate_key(best)
        return None

    def _add_row(rate, net_val=None, vat_val=None, gross_val=None):
        key = _normalize_vat_rate_key(rate)
        if not key:
            return
        if net_val is None and vat_val is None and gross_val is None:
            return
        row = rows.setdefault(key, {"net": 0.0, "vat": 0.0, "gross": 0.0})
        if net_val is not None:
            row["net"] += net_val
        if vat_val is not None:
            row["vat"] += vat_val
        if gross_val is not None:
            row["gross"] += gross_val

    # Table-based extraction (best effort)
    for tr in soup.find_all("tr"):
        txt = tr.get_text(" ", strip=True)
        if not txt:
            continue
        low = txt.lower()
        if "%" not in txt:
            continue
        if not any(tok in low for tok in ("φπα", "fpa", "vat", "tax")):
            continue
        m_rate = re.search(r"(\d{1,2}(?:[\.,]\d+)?)\s*%", txt)
        if not m_rate:
            continue
        amounts = [
            _amount_to_float(m.group(1))
            for m in re.finditer(r"(-?\d{1,3}(?:[\.,]\d{3})*(?:[\.,]\d+)?|\d+(?:[\.,]\d+)?)", txt)
        ]
        amounts = [a for a in amounts if a is not None]
        if not amounts:
            continue
        net_val = vat_val = gross_val = None
        if len(amounts) >= 3:
            net_val, vat_val, gross_val = amounts[-3], amounts[-2], amounts[-1]
        elif len(amounts) == 2:
            net_val, vat_val = amounts[-2], amounts[-1]
            gross_val = net_val + vat_val
        else:
            gross_val = amounts[-1]
        _add_row(m_rate.group(1), net_val, vat_val, gross_val)

    # VAT-summary-table extraction (e.g. "Ανάλυση Φ.Π.Α.")
    if not rows:
        for table in soup.find_all("table"):
            table_text = table.get_text(" ", strip=True)
            if not re.search(r"φ\.?π\.?α\.?|vat", table_text, re.I):
                continue
            if not re.search(r"%", table_text):
                continue
            if not re.search(r"αν[άα]λυση|analysis|καθαρ[όο]\s*ποσ[όο]\s*αν[άα]\s*φ\.?π\.?α\.?|αξ[ίι]α\s*φ\.?π\.?α\.?|tax\s*subtotal|tax\s*summary", table_text, re.I):
                continue

            for tr in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if len(cells) < 2:
                    continue
                row_txt = " ".join(cells)
                # skip header-like rows
                if re.search(r"καθαρ|ποσό|ανάλυση|φ\.?π\.?α\.?|vat", row_txt, re.I) and not re.search(r"\d", row_txt):
                    continue

                rate = None
                for cell in cells:
                    m_rate = re.search(r"(\d{1,2}(?:[\.,]\d+)?)\s*%", cell)
                    if m_rate:
                        rate = m_rate.group(1)
                        break
                if rate is None:
                    for cell in cells:
                        rate_val = _amount_to_float(cell)
                        if rate_val is not None and 0 <= rate_val <= 100:
                            rate = str(rate_val)
                            break
                if rate is None:
                    continue

                numeric_vals = [_amount_to_float(c) for c in cells]
                numeric_vals = [v for v in numeric_vals if v is not None]
                net_val = vat_val = None
                if len(numeric_vals) >= 2:
                    net_val, vat_val = numeric_vals[-2], numeric_vals[-1]
                elif len(numeric_vals) == 1:
                    vat_val = numeric_vals[0]
                gross_val = None
                if net_val is not None and vat_val is not None:
                    gross_val = net_val + vat_val
                _add_row(rate, net_val, vat_val, gross_val)

    # Text fallback for patterns like "ΦΠΑ 24%"
    if not rows and html_text:
        for line in re.split(r"[\n\r]+", html_text):
            low = line.lower()
            if not any(tok in low for tok in ("φπα", "fpa", "vat", "tax")):
                continue
            m_rate = re.search(r"(\d{1,2}(?:[\.,]\d+)?)\s*%", line)
            if not m_rate:
                continue
            amounts = [
                _amount_to_float(m.group(1))
                for m in re.finditer(r"(-?\d{1,3}(?:[\.,]\d{3})*(?:[\.,]\d+)?|\d+(?:[\.,]\d+)?)", line)
            ]
            amounts = [a for a in amounts if a is not None]
            if not amounts:
                continue
            if len(amounts) >= 2:
                net_val, vat_val = amounts[-2], amounts[-1]
                _add_row(m_rate.group(1), net_val, vat_val, net_val + vat_val)
            else:
                _add_row(m_rate.group(1), None, None, amounts[-1])

    # AADE receipt table fallback (Καθαρή αξία Α-Ε + ΦΠΑ Α-Δ, without explicit %)
    if not rows:
        net_by_cat = {}
        vat_by_cat = {}
        cat_rate_map = {
            # AADE receipt layout (Καθαρή αξία Α-Ε / ΦΠΑ Α-Δ):
            # Α=6%, Β=13%, Γ=24%, Δ=36%, Ε=0%
            "A": "6", "B": "13", "C": "24", "D": "36", "E": "0",
            "Α": "6", "Β": "13", "Γ": "24", "Δ": "36", "Ε": "0",
        }
        greek_to_latin = {"Α": "A", "Β": "B", "Γ": "C", "Δ": "D", "Ε": "E"}

        for tr in soup.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) < 2:
                continue
            label = tds[0].get_text(" ", strip=True)
            val_txt = tds[1].get_text(" ", strip=True)
            if not label:
                continue

            m_net = re.search(r"καθαρ[ήη]\s*αξ[ίι]α\s*([A-EΑ-Ε])", label, re.I)
            m_vat = re.search(r"φπα\s*([A-EΑ-Ε])", label, re.I)
            amount_val = _amount_to_float(val_txt)
            if amount_val is None:
                continue

            if m_net:
                cat = m_net.group(1).upper()
                cat = greek_to_latin.get(cat, cat)
                net_by_cat[cat] = amount_val
            elif m_vat:
                cat = m_vat.group(1).upper()
                cat = greek_to_latin.get(cat, cat)
                vat_by_cat[cat] = amount_val

        for cat in sorted(set(net_by_cat.keys()) | set(vat_by_cat.keys())):
            net_val = net_by_cat.get(cat)
            vat_val = vat_by_cat.get(cat)
            if (net_val is None and vat_val is None) or ((net_val or 0.0) == 0.0 and (vat_val or 0.0) == 0.0):
                continue

            rate = cat_rate_map.get(cat)
            if rate is None:
                rate = _guess_rate_from_amounts(net_val, vat_val)
                if rate is not None:
                    inferred_used = True
            else:
                # Για το AADE table οι κατηγορίες Α-Ε είναι ρητή ανάλυση ΦΠΑ,
                # άρα ΔΕΝ το μαρκάρουμε ως inferred.
                pass

            if rate is not None:
                gross_val = (net_val or 0.0) + (vat_val or 0.0)
                _add_row(rate, net_val, vat_val, gross_val)

    # Field-based fallback for pages that expose only net/vat/total without explicit rate
    if not rows:
        net_raw = _extract_input_or_text(soup, "namount", "netAmount", "net_amount", "netvalue", "netValue", "taxableAmount")
        vat_raw = _extract_input_or_text(soup, "vat", "vatamount", "vat_amount", "taxAmount", "tax_amount", "totalVatAmount")
        gross_raw = _extract_input_or_text(soup, "tamount", "t_amount", "totalAmount", "total_amount", "totalGrossValue")

        net_val = _amount_to_float(net_raw)
        vat_val = _amount_to_float(vat_raw)
        gross_val = _amount_to_float(gross_raw)
        if gross_val is None and net_val is not None and vat_val is not None:
            gross_val = net_val + vat_val

        guessed_rate = _guess_rate_from_amounts(net_val, vat_val)
        if guessed_rate is not None:
            inferred_used = True
            _add_row(guessed_rate, net_val, vat_val, gross_val)

    result = {
        k: {
            "net_amount": _float_to_comma(v.get("net")),
            "vat_amount": _float_to_comma(v.get("vat")),
            "gross_amount": _float_to_comma(v.get("gross")),
        }
        for k, v in rows.items()
    }
    if result:
        result["__inferred__"] = inferred_used
    return result

def _merge_vat_analysis(out_dict, new_analysis):
    if not isinstance(out_dict, dict):
        return
    if not isinstance(new_analysis, dict) or not new_analysis:
        return
    inferred_flag = bool(new_analysis.get("__inferred__", False)) if isinstance(new_analysis, dict) else False
    existing = out_dict.get("vat_analysis")
    if not isinstance(existing, dict):
        cleaned = {k: v for k, v in new_analysis.items() if k != "__inferred__"}
        out_dict["vat_analysis"] = cleaned
        out_dict["vat_analysis_inferred"] = inferred_flag
        return

    out_dict["vat_analysis_inferred"] = bool(out_dict.get("vat_analysis_inferred", False) or inferred_flag)

    for rate, vals in new_analysis.items():
        if rate == "__inferred__":
            continue
        if rate not in existing:
            existing[rate] = vals
            continue
        old_vals = existing.get(rate, {})
        sums = {
            "net_amount": _amount_to_float(old_vals.get("net_amount")),
            "vat_amount": _amount_to_float(old_vals.get("vat_amount")),
            "gross_amount": _amount_to_float(old_vals.get("gross_amount")),
        }
        adds = {
            "net_amount": _amount_to_float(vals.get("net_amount")),
            "vat_amount": _amount_to_float(vals.get("vat_amount")),
            "gross_amount": _amount_to_float(vals.get("gross_amount")),
        }
        merged = {}
        for key in ("net_amount", "vat_amount", "gross_amount"):
            left = sums.get(key)
            right = adds.get(key)
            if left is None and right is None:
                merged[key] = None
            else:
                merged[key] = _float_to_comma((left or 0.0) + (right or 0.0))
        existing[rate] = merged

def _ensure_vat_analysis(out_dict):
    if isinstance(out_dict, dict) and "vat_analysis" not in out_dict:
        out_dict["vat_analysis"] = None
    if isinstance(out_dict, dict) and "vat_analysis_inferred" not in out_dict:
        out_dict["vat_analysis_inferred"] = False

def _reconcile_single_rate_vat_with_total(target):
    if not isinstance(target, dict):
        return
    vmap = target.get("vat_analysis")
    if not isinstance(vmap, dict) or not vmap:
        return
    total_val = _amount_to_float(target.get("total_amount"))
    if total_val is None or total_val <= 0:
        return

    rows = [(k, v) for k, v in vmap.items() if k != "__inferred__" and isinstance(v, dict)]
    if len(rows) != 1:
        return

    rate_key, row = rows[0]
    gross_val = _amount_to_float(row.get("gross_amount"))
    if gross_val is None:
        net_val = _amount_to_float(row.get("net_amount"))
        vat_val = _amount_to_float(row.get("vat_amount"))
        if net_val is not None and vat_val is not None:
            gross_val = net_val + vat_val

    if gross_val is None:
        return

    # Reconcile even for small totals: a 1 EUR tolerance is too loose for
    # low-value invoices/credit notes.
    if abs(gross_val - total_val) <= max(0.06, total_val * 0.02):
        return

    try:
        rate_num = float(str(rate_key).replace(",", "."))
    except Exception:
        return
    if rate_num < 0 or rate_num > 100:
        return

    denom = 1.0 + (rate_num / 100.0)
    if denom <= 0:
        return

    net_new = total_val / denom
    vat_new = total_val - net_new
    vmap[rate_key] = {
        "net_amount": _float_to_comma(net_new),
        "vat_amount": _float_to_comma(vat_new),
        "gross_amount": _float_to_comma(total_val),
    }
    target["vat_analysis_inferred"] = True


def _infer_vat_analysis_from_total(target):
    """Best-effort fallback when source page has total but no VAT rows.

    This intentionally marks output as inferred.
    """
    if not isinstance(target, dict):
        return
    if isinstance(target.get("vat_analysis"), dict) and target.get("vat_analysis"):
        return

    total_val = _amount_to_float(target.get("total_amount"))
    if total_val is None or total_val <= 0:
        return

    doc_text = " ".join(
        str(x or "")
        for x in (target.get("doc_type"), target.get("source"), target.get("progressive_aa"))
    )

    # For retail receipt-like docs (e.g. 11.1 / ΑΛΠ) default to 24% as
    # a practical fallback only when nothing explicit is available.
    if not re.search(r"\b11\.1\b|αλπ|απόδειξ|αποδειξ|λιανικ|receipt", doc_text, re.I):
        return

    rate_key = "24"
    denom = 1.0 + (float(rate_key) / 100.0)
    net_val = total_val / denom
    vat_val = total_val - net_val

    target["vat_analysis"] = {
        rate_key: {
            "net_amount": _float_to_comma(net_val),
            "vat_amount": _float_to_comma(vat_val),
            "gross_amount": _float_to_comma(total_val),
        }
    }
    target["vat_analysis_inferred"] = True

def _normalize_mark_value(value):
    m = MARK_RE.search(str(value or "").strip())
    return m.group(0) if m else None

def _normalize_invoice_flag(result):
    if not isinstance(result, dict):
        return
    # Hard rule requested: if "τιμολόγιο" appears anywhere in textual
    # result fields, always mark as invoice.
    text_blob = " ".join(str(v) for v in result.values() if isinstance(v, str))
    if re.search(r"τιμολογ", text_blob, re.I):
        result["is_invoice"] = True
        return

    doc_type_text = str(result.get("doc_type") or "")
    if not doc_type_text:
        return
    low = doc_type_text.lower()
    if re.search(r"απόδειξ|αποδειξ|λιανικ|receipt", low, re.I):
        result["is_invoice"] = False
        return
    if re.search(r"τιμολογ|invoice|credit\s*note|πιστωτικ", low, re.I):
        result["is_invoice"] = True

def _finalize_detect_result(result):
    if isinstance(result, dict):
        _ensure_vat_analysis(result)
        _reconcile_single_rate_vat_with_total(result)
        result["MARK"] = _normalize_mark_value(result.get("MARK"))
        _normalize_invoice_flag(result)
    return result

def _text_of(el):
    if not el:
        return ""
    if hasattr(el, "get_text"):
        return el.get_text(" ", strip=True)
    return str(el).strip()

def _clean_issuer_name(raw):
    if raw is None:
        return None
    text = re.sub(r"\s+", " ", str(raw)).strip(" \t\r\n:-")
    if not text:
        return None
    low = text.lower()
    noise_markers = [
        "ευχαριστούμε που χρησιμοποιείτε τις υπηρεσίες",
        "αφμ εκδότη",
        "διεύθυνση εκδότη",
        "επωνυμία πελάτη",
        "αφμ πελάτη",
        "επάγγελμα",
    ]
    if sum(marker in low for marker in noise_markers) >= 2:
        return None
    if low in {
        "εκδότη",
        "επωνυμία",
        "επωνυμία εκδότη",
        "supplier",
        "seller",
        "issuer",
    }:
        return None
    return text

def _extract_input_or_text(soup, *ids_or_names):
    for key in ids_or_names:
        if not key:
            continue
        el = soup.find(id=key)
        if el:
            val = el.get("value") or el.get_text(" ", strip=True)
            if val and str(val).strip():
                return str(val).strip()
        el2 = soup.find(attrs={"name": key})
        if el2:
            val = el2.get("value") or el2.get_text(" ", strip=True)
            if val and str(val).strip():
                return str(val).strip()
        sel = soup.select_one(f"#{key}")
        if sel:
            t = sel.get_text(" ", strip=True)
            if t:
                return t
    return None

def _extract_mydatapi_url_from_text(text, base_url=None):
    if not text:
        return None
    normalized = str(text)
    normalized = normalized.replace('\\/', '/').replace('\\u002F', '/').replace('\\u003D', '=')
    normalized = normalized.replace('&amp;', '&')

    patterns = [
        r'https?://mydatapi\.aade\.gr/[^\s"\'<>]*TimologioQR/QRInfo\?q=[^\s"\'<>]+',
        r'/(?:myDATA|mydata)/TimologioQR/QRInfo\?q=[^\s"\'<>]+'
    ]
    for pat in patterns:
        m = re.search(pat, normalized, re.I)
        if not m:
            continue
        candidate = m.group(0).strip('"\' )>;')
        if candidate.startswith('/'):
            if base_url:
                candidate = urljoin(base_url, candidate)
            else:
                continue
        return candidate
    return None

def _extract_xml_fragment(text):
    if not text:
        return None
    patterns = [
        r'(<\?xml[^<]*<InvoicesDoc[\s\S]*?</InvoicesDoc>)',
        r'(<\?xml[^<]*<invoice[\s\S]*?</invoice>)',
        r'(<InvoicesDoc[\s\S]*?</InvoicesDoc>)',
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            return m.group(1)
    return None

def _extract_vat_from_primer_xml(root, seller_vat=None):
    candidates = []
    for el in root.iter():
        tag = el.tag.split('}')[-1].lower()
        if tag in ('vatnumber', 'vat', 'afm', 'companyid'):
            txt = (el.text or '').strip()
            if txt:
                m = VAT_RE.search(txt)
                if m:
                    candidates.append(m.group(0))
    if seller_vat:
        for vat in candidates:
            if vat != seller_vat:
                return vat
    if candidates:
        return candidates[-1]
    return None

def _extract_mark_from_primer_xml(root):
    for el in root.iter():
        tag = el.tag.split('}')[-1].lower()
        if tag == 'mark':
            txt = (el.text or '').strip()
            if txt and re.fullmatch(r'\d{15}', txt):
                return txt
    return None

def _extract_from_jsonld(soup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string)
        except Exception:
            continue
        if isinstance(data, dict):
            yield data
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield item

def _xml_text(root, xpath):
    el = root.find(xpath)
    return el.text.strip() if el is not None and el.text else None

def _ns_strip(tag):
    return tag.split("}")[-1] if "}" in tag else tag

def _extract_from_ubl_root(root):
    """Extract seller (issuer) info from UBL-like XML (namespace-agnostic)."""
    out = {"issuer_vat": None, "issuer_name": None, "issue_date": None,
           "progressive_aa": None, "doc_type": None, "total_amount": None, "MARK": None}
    # find supplier / issuer
    supplier = None
    for candidate in ("AccountingSupplierParty","SellerSupplierParty","SupplierParty","AccountingCustomerParty"):
        el = root.find(".//{*}" + candidate)
        if el is not None:
            supplier = el
            break
    if supplier is None:
        # fallback look for Party with VAT tags
        for el in root.findall(".//"):
            tagname = _ns_strip(el.tag).lower()
            if tagname in ("accountingsupplierparty","supplierparty","sellerparty"):
                supplier = el
                break
    if supplier is not None:
        # vat search inside supplier
        for sub in supplier.iter():
            tag = _ns_strip(sub.tag).lower()
            txt = (sub.text or "").strip()
            if tag in ("companyid","vatnumber","vat_number","vat"):
                m = re.search(r"(\d{9,})", txt)
                if m:
                    out["issuer_vat"] = m.group(1)
                    break
        # name
        name_el = supplier.find(".//{*}Name") or supplier.find(".//{*}CompanyName") or supplier.find(".//{*}RegistrationName")
        if name_el is not None and (name_el.text or "").strip():
            out["issuer_name"] = name_el.text.strip()
    # issue date
    for tag in ("IssueDate","IssueTime","DocumentDate"):
        d = root.find(".//{*}" + tag)
        if d is not None and (d.text or "").strip():
            out["issue_date"] = _norm_date_to_ddmmyyyy(d.text.strip())
            break
    # total amount
    for tag in ("LegalMonetaryTotal","LegalTotal","MonetaryTotal","TotalAmount"):
        el = root.find(".//{*}" + tag)
        if el is not None:
            # try PayableAmount or Payable
            pa = el.find(".//{*}PayableAmount") or el.find(".//{*}Payable") or el.find(".//{*}Amount") or el.find(".//{*}PayableAmount")
            if pa is not None and (pa.text or "").strip():
                out["total_amount"] = _clean_amount_to_comma(pa.text.strip())
                break
    if not out["total_amount"]:
        # try any element named PayableAmount
        el = root.find(".//{*}PayableAmount")
        if el is not None and (el.text or "").strip():
            out["total_amount"] = _clean_amount_to_comma(el.text.strip())
    # doc type / invoice type
    itc = root.find(".//{*}InvoiceTypeCode") or root.find(".//{*}DocumentType")
    if itc is not None and (itc.text or "").strip():
        out["doc_type"] = itc.text.strip()
    # ID / progressive no
    id_el = root.find(".//{*}ID") or root.find(".//{*}InvoiceNumber")
    if id_el is not None and (id_el.text or "").strip():
        out["progressive_aa"] = id_el.text.strip()
    # MARK in xml if any
    for el in root.findall(".//{*}mark") + root.findall(".//{*}Mark") + root.findall(".//{*}DocumentReference"):
        t = (el.text or "").strip()
        if t and MARK_RE.search(t):
            out["MARK"] = MARK_RE.search(t).group(0)
            break
    return out

# ---------- specialized scrapers (return unified dict) ----------

def scrape_www1_aade(url, timeout=15, debug=False):
    """
    Scrape pages like:
    https://www1.aade.gr/tameiakes/myweb/q1.php?SIG=...
    Return unified dict.
    """
    res = {"issuer_vat": None, "issue_date": None, "issuer_name": None,
           "progressive_aa": None, "doc_type": None, "total_amount": None,
           "is_invoice": False, "MARK": None, "source": "AADE_www1", "vat_analysis": None,
           "vat_analysis_inferred": False}
    try:
        html = _fetch_url_text(url, headers=HEADERS, timeout=timeout, debug=debug)
    except Exception as e:
        if debug: print("www1 fetch error:", e)
        return res

    soup = BeautifulSoup(html, "html.parser")
    # parse table rows where first td is label, second td is value
    for tr in soup.select("table.info tr"):
        tds = tr.find_all(["td","th"])
        if len(tds) < 2:
            continue
        label = tds[0].get_text(" ", strip=True)
        val = tds[1].get_text(" ", strip=True)
        if re.search(r"Συνολική αξία|Συνολικό ποσό|Συνολικού ποσού", label, re.I):
            res["total_amount"] = _clean_amount_to_comma(val)
        elif re.search(r"Ημερομηνία", label, re.I):
            # sample "2023-07-31 16:28" -> dd/mm/yyyy
            res["issue_date"] = _norm_date_to_ddmmyyyy(val)
        elif re.search(r"ΑΦΜ εκδότη|ΑΦΜ.*εκδότη", label, re.I):
            m = re.search(r"(\d{9,})", val)
            if m: res["issuer_vat"] = m.group(1)
        elif re.search(r"Επωνυμία", label, re.I):
            res["issuer_name"] = val.strip()
        elif re.search(r"Προοδευτικός α\/α|Προοδευτικ", label, re.I):
            res["progressive_aa"] = val.strip()
        elif re.search(r"Είδος παραστατικού", label, re.I):
            res["doc_type"] = val.strip()
            if re.search(r"τιμολόγι", val, re.I):
                res["is_invoice"] = True
        # MARK could be absent; try rows or paragraphs earlier
        if not res["MARK"]:
            m_mark = MARK_RE.search(val)
            if m_mark:
                res["MARK"] = m_mark.group(0)
    # VAT analysis (best effort)
    _merge_vat_analysis(res, _extract_vat_breakdown_from_html(soup, html))

    # fallback searches across page if some fields missing
    if not res["issuer_vat"]:
        m = VAT_RE.search(html)
        if m: res["issuer_vat"] = m.group(0)
    if not res["total_amount"]:
        # text near euro symbol
        m = re.search(r"€\s*([0-9\.,]+)", html)
        if m:
            res["total_amount"] = _clean_amount_to_comma(m.group(1))
    return res

def scrape_mydatapi(url, timeout=12, debug=False):
    """
    Robust extractor for mydatapi pages (id attributes, json-ld, etc.)
    """
    out = {"issuer_vat": None, "issue_date": None, "issuer_name": None,
           "progressive_aa": None, "doc_type": None, "total_amount": None,
           "is_invoice": False, "MARK": None, "source": "MyData", "vat_analysis": None,
           "vat_analysis_inferred": False}
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        html = r.text
    except Exception as e:
        if debug: print("mydatapi fetch error:", e)
        return out

    soup = BeautifulSoup(html, "html.parser")
    # MARK
    mark = _extract_input_or_text(soup, "tmark", "mark", "mark_id", "markNumber")
    if mark: out["MARK"] = mark.strip()
    # doc type
    doc_type = _extract_input_or_text(soup, "dtype", "doc_type", "documentType", "document_type")
    if doc_type:
        out["doc_type"] = doc_type.strip()
        if re.search(r"τιμολό?γιο|τιμολογιο|τιμολόγιο", doc_type, re.I):
            out["is_invoice"] = True
    # total
    total = _extract_input_or_text(soup, "tamount", "t_amount", "tamount_total", "tamount")
    if not total:
        label = soup.find(string=re.compile(r"Συνολικού ποσού|Συνολική αξία|Συνολικό ποσό", re.I))
        if label:
            parent = label.find_parent()
            if parent:
                nxt = parent.find_next(["input","strong","td","span"])
                if nxt: total = nxt.get("value") or nxt.get_text(" ", strip=True)
    if total:
        out["total_amount"] = _clean_amount_to_comma(total)
    # issue date
    date_raw = _extract_input_or_text(soup, "tdate", "t_date", "issueDate", "tdate")
    if not date_raw:
        lab = soup.find(string=re.compile(r"Ημερομηνία.*Έκδοσης|Ημερομηνία, ώρα", re.I))
        if lab:
            p = lab.find_parent()
            if p:
                nxt = p.find_next(["input","td","span","div"])
                if nxt:
                    date_raw = nxt.get("value") or nxt.get_text(" ", strip=True)
    out["issue_date"] = _norm_date_to_ddmmyyyy(date_raw) if date_raw else None
    # issuer vat
    vat_raw = _extract_input_or_text(soup, "vatnumber", "vat_number", "issuer_vat", "crvatnumber", "companyid", "vat")
    if not vat_raw:
        label = soup.find(string=re.compile(r"Α\.?Φ\.?Μ.*εκδότη|ΑΦΜ εκδότη|ΑΦΜ", re.I))
        if label:
            p = label.find_parent()
            if p:
                nxt = p.find_next(["input","td","span"])
                if nxt:
                    vat_raw = nxt.get("value") or nxt.get_text(" ", strip=True)
    if vat_raw:
        m = re.search(r"(\d{9,})", str(vat_raw))
        if m: out["issuer_vat"] = m.group(1)
        else: out["issuer_vat"] = re.sub(r"\D", "", str(vat_raw)) or None
    # issuer name
    iname = _extract_input_or_text(soup, "bname", "issuer_name", "issuer", "companyName", "businessName")
    if not iname:
        label = soup.find(string=re.compile(r"Επωνυμία\s*(εκδότη)?|Επωνυμία εκδότη", re.I))
        if label:
            p = label.find_parent()
            if p:
                nxt = p.find_next(["input","td","span"])
                if nxt:
                    iname = nxt.get("value") or nxt.get_text(" ", strip=True)
    out["issuer_name"] = _clean_issuer_name(iname)
    # progressive aa
    paa = _extract_input_or_text(soup, "saa", "s_aa", "saa", "saa_input", "s_aa")
    if not paa:
        lab = soup.find(string=re.compile(r"Προοδευτικ(ός|ο)\s*α\/α|Προοδευτικός", re.I))
        if lab:
            p = lab.find_parent()
            if p:
                nxt = p.find_next(["input","td","span"])
                if nxt:
                    paa = nxt.get("value") or nxt.get_text(" ", strip=True)
    out["progressive_aa"] = (str(paa).strip() if paa else None)
    # try JSON-LD fallback
    if not (out["issuer_vat"] and out["issue_date"] and out["total_amount"]):
        for obj in _extract_from_jsonld(soup):
            try:
                if not out["issuer_vat"]:
                    comp = obj.get("seller") or obj.get("provider") or obj.get("sellerOrganization")
                    if comp and isinstance(comp, dict):
                        vatc = comp.get("vatNumber") or comp.get("taxID") or comp.get("vat")
                        if vatc:
                            mm = re.search(r"(\d{9,})", str(vatc))
                            if mm: out["issuer_vat"] = mm.group(1)
                if not out["issue_date"]:
                    idate = obj.get("dateIssued") or obj.get("issueDate")
                    if idate: out["issue_date"] = _norm_date_to_ddmmyyyy(idate)
                if not out["total_amount"]:
                    tam = obj.get("totalPaymentDue") or obj.get("totalPrice") or obj.get("total")
                    if isinstance(tam, dict):
                        tamv = tam.get("amount")
                    else:
                        tamv = tam
                    if tamv: out["total_amount"] = _clean_amount_to_comma(tamv)
                if not out["issuer_name"]:
                    comp = obj.get("seller") or obj.get("provider") or obj.get("sellerOrganization")
                    if isinstance(comp, dict):
                        iname_c = comp.get("name") or comp.get("legalName")
                        if iname_c:
                            out["issuer_name"] = _clean_issuer_name(iname_c)
            except Exception:
                continue

    page_text = soup.get_text(" ", strip=True)
    if not out["doc_type"]:
        m_doc = re.search(r"(?:Είδος\s*Παραστατικού|Type|Document|Invoice\s*Type)[\s:]*([^\n<]+)", page_text, re.I)
        if m_doc:
            out["doc_type"] = m_doc.group(1).strip()
    if not out["total_amount"]:
        m_total = re.search(r"(?:Σύνολο|Συνολική αξία|Συνολικό ποσό|Πληρωτέο ποσό|Amount\s*Due|Total)[\s:]*€?\s*([0-9][0-9\.,]+)", page_text, re.I)
        if m_total:
            out["total_amount"] = _clean_amount_to_comma(m_total.group(1))
    if not out["issue_date"]:
        m_date = re.search(r"(?:Ημερομηνία.*Έκδοσης|Ημερομηνία|IssueDate|DateIssued|Issue\s*Date)[\s:]*([0-9]{1,2}[\/\-\.][0-9]{1,2}[\/\-\.][0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2})", page_text, re.I)
        if m_date:
            out["issue_date"] = _norm_date_to_ddmmyyyy(m_date.group(1))
    if not out["issuer_vat"]:
        m_vat = re.search(r"Α\.?Φ\.?Μ\.?[:\s]*([0-9]{9})", page_text, re.I)
        if m_vat:
            out["issuer_vat"] = m_vat.group(1)
    if not out["issuer_name"]:
        m_name = re.search(r"(?:Επωνυμία|Supplier|Seller|Issuer)[\s:]+([^\n<]+)", page_text, re.I)
        if m_name:
            out["issuer_name"] = _clean_issuer_name(m_name.group(1))
    if not out["progressive_aa"]:
        m_aa = re.search(r"(?:Αρ\.\?\s*Παραστατικού|Προοδευτικ(?:ός|ο)\s*α\/?α|Invoice\s*No|ΑΑ)[\s:]*([A-Za-z0-9\-_/]+)", page_text, re.I)
        if m_aa:
            out["progressive_aa"] = m_aa.group(1).strip()
    if not out["MARK"]:
        m_mark = MARK_RE.search(page_text)
        if m_mark:
            out["MARK"] = m_mark.group(0)

    out["issuer_name"] = _clean_issuer_name(out.get("issuer_name"))

    _merge_vat_analysis(out, _extract_vat_breakdown_from_html(soup, html))
    _reconcile_single_rate_vat_with_total(out)
    _infer_vat_analysis_from_total(out)

    if out["doc_type"] and re.search(r"τιμολό?γιο|τιμολογιο|τιμολόγιο", out["doc_type"], re.I):
        out["is_invoice"] = True
    return out

def scrape_wedoconnect(url, timeout=20, debug=False):
    """
    Modified wedoconnect scraper:
    - Προσπαθεί να βρει XML attachments/UBL και να εξάγει πεδία.
    - Εάν το XML περιέχει <cbc:ID> (ή άλλο ID) με pipe-separated segments
      (π.χ. "094222211|18/08/2025|51|1.1|ΤΔ147|000580206"), θα:
        - πάρει πιθανό ΑΦΜ από το πρώτο segment (αν είναι 9-digit),
        - πάρει ημερομηνία από δεύτερο segment (αν μοιάζει με ημερομηνία),
        - πάρει doc_type από segment που μοιάζει σε μορφή X.Y (π.χ. "1.1" ή "13.1"),
        - πάρει progressive_aa από το τελευταίο segment αν είναι αριθμητικό.
    - Διατηρεί τα υπάρχοντα fallbacks (xml parsing μέσω _extract_from_ubl_root, HTML scanning).
    """
    out = {"issuer_vat": None, "issue_date": None, "issuer_name": None,
           "progressive_aa": None, "doc_type": None, "total_amount": None,
           "is_invoice": False, "MARK": None, "source": "Wedoconnect", "vat_analysis": None,
           "vat_analysis_inferred": False}
    sess = requests.Session()
    sess.headers.update(HEADERS)

    parsed_in = urlparse(url)
    fetch_urls = [url]
    if "vs.gr" in (parsed_in.netloc or "").lower() and "/iv/invoice/" in (parsed_in.path or "").lower():
        q = parse_qs(parsed_in.query)
        if str(q.get("peppol", [""])[0]).lower() != "true":
            sep = "&" if parsed_in.query else "?"
            peppol_url = f"{url}{sep}peppol=true"
            fetch_urls = [peppol_url, url]

    last_error = None
    r = None
    html = None
    used_url = None
    for candidate_url in fetch_urls:
        try:
            rr = sess.get(candidate_url, timeout=timeout)
            rr.raise_for_status()
            candidate_html = rr.text
            if ("peppol=true" in candidate_url) and ("Ημερομηνία Έκδοσης" not in candidate_html) and ("Ανάλυση Φ.Π.Α." not in candidate_html):
                continue
            r = rr
            html = candidate_html
            used_url = candidate_url
            break
        except Exception as e:
            last_error = e

    if r is None or html is None:
        if debug: print("wedoconnect fetch error:", last_error)
        return out
    if debug and used_url and used_url != url:
        print("wedoconnect using URL:", used_url)

    soup = BeautifulSoup(html, "html.parser")
    # quick MARK from page
    m_mark = MARK_RE.search(html)
    if m_mark: out["MARK"] = m_mark.group(0)

    # find candidate xml/ubl links as before
    candidate_urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(r.url, href)
        low = href.lower()
        txt = (a.get("title") or a.get("download") or a.text or "").lower()
        if any(tok in low for tok in (".xml", "az-ubl", "az_ubl", "ubl", "mydatafilecontainer", "mydata")):
            candidate_urls.append(full)
        elif any(tok in txt for tok in ("az-ubl", "mydata", "ubl", ".xml")):
            candidate_urls.append(full)
    # iframe/embed
    for iframe in soup.find_all("iframe", src=True):
        candidate_urls.append(urljoin(r.url, iframe["src"]))
    for emb in soup.find_all("embed", src=True):
        candidate_urls.append(urljoin(r.url, emb["src"]))
    seen = set()
    candidate_urls = [u for u in candidate_urls if not (u in seen or seen.add(u))]

    # helper to try parse pipe-separated cbc:ID inside an XML root
    def _try_parse_pipe_id_from_root(root):
        # search all ID elements and prefer ones containing '|'
        for id_el in root.findall(".//{*}ID") + root.findall(".//ID"):
            txt = (id_el.text or "").strip()
            if '|' in txt:
                parts = [p.strip() for p in txt.split('|')]
                # attempt mappings: first segment AFM, second date, any \d+\.\d+ as doc_type, last numeric as progressive_aa
                found = {}
                # AFM candidate (first)
                if parts and re.match(r"^\d{9}$", parts[0]):
                    found["issuer_vat"] = parts[0]
                # date candidate (try second)
                if len(parts) > 1:
                    dt = _fmt_date_to_ddmmyyyy(parts[1])
                    if dt:
                        found["issue_date"] = dt
                # doc_type candidate: look for first segment matching \d+\.\d+ or containing '13.' etc.
                doc_candidate = None
                for p in parts:
                    if re.search(r"\d+\.\d+", p):
                        doc_candidate = p
                        break
                if doc_candidate:
                    found["doc_type"] = doc_candidate
                # progressive aa: prefer last segment if numeric
                last = parts[-1]
                if re.search(r"\d{2,}", last):
                    found["progressive_aa"] = re.sub(r"\D", "", last)
                # also try to detect issuer_vat inside any segment if not found
                if "issuer_vat" not in found:
                    for p in parts:
                        m = re.search(r"(\d{9})", p)
                        if m:
                            found["issuer_vat"] = m.group(1)
                            break
                return found
        return None

    # try attachments first (prefer XML attachments)
    parsed_xml_found = False
    for cu in candidate_urls:
        try:
            r2 = sess.get(cu, timeout=timeout)
            r2.raise_for_status()
            content = r2.content
            text = r2.text
        except Exception:
            continue
        ctype = (r2.headers.get("Content-Type") or "").lower()
        if "xml" in ctype or b"<?xml" in content[:200].lower() or re.search(r"<(Invoice|InvoicesDoc|cbc:Invoice)\b", text, flags=re.I):
            try:
                root = ET.fromstring(content)
            except Exception:
                try:
                    txt = content.decode("utf-8", errors="replace")
                    idx = txt.find("<?xml")
                    if idx != -1:
                        root = ET.fromstring(txt[idx:].encode("utf-8"))
                    else:
                        continue
                except Exception:
                    continue
            # 1) try the special pipe-ID parsing
            parsed = _try_parse_pipe_id_from_root(root)
            if parsed:
                # apply parsed values
                if parsed.get("issuer_vat") and not out.get("issuer_vat"):
                    out["issuer_vat"] = parsed.get("issuer_vat")
                if parsed.get("issue_date") and not out.get("issue_date"):
                    out["issue_date"] = parsed.get("issue_date")
                if parsed.get("doc_type") and not out.get("doc_type"):
                    out["doc_type"] = parsed.get("doc_type")
                if parsed.get("progressive_aa") and not out.get("progressive_aa"):
                    out["progressive_aa"] = parsed.get("progressive_aa")
            # 2) then fallback to generic UBL extraction
            extracted = _extract_from_ubl_root(root)
            for k, v in extracted.items():
                if v and not out.get(k):
                    out[k] = v
            _merge_vat_analysis(out, _extract_vat_breakdown_from_xml_root(root))
            if out.get("MARK") is None and extracted.get("MARK"):
                out["MARK"] = extracted["MARK"]
            parsed_xml_found = True
            # if we have essential fields stop
            if out.get("issuer_vat") and out.get("total_amount"):
                break

    # If no attachments or fields remain missing, try inline XML fragments in HTML
    if not parsed_xml_found:
        # try to find inline xml in page text
        for m in re.finditer(r'(<\?xml[\s\S]{0,20000}?</(?:Invoice|InvoicesDoc|UBLInvoice)>)', html, flags=re.I):
            snippet = m.group(1)
            try:
                root = ET.fromstring(snippet.encode("utf-8"))
                parsed = _try_parse_pipe_id_from_root(root)
                if parsed:
                    if parsed.get("issuer_vat") and not out.get("issuer_vat"):
                        out["issuer_vat"] = parsed.get("issuer_vat")
                    if parsed.get("issue_date") and not out.get("issue_date"):
                        out["issue_date"] = parsed.get("issue_date")
                    if parsed.get("doc_type") and not out.get("doc_type"):
                        out["doc_type"] = parsed.get("doc_type")
                    if parsed.get("progressive_aa") and not out.get("progressive_aa"):
                        out["progressive_aa"] = parsed.get("progressive_aa")
                extracted = _extract_from_ubl_root(root)
                for k, v in extracted.items():
                    if v and not out.get(k):
                        out[k] = v
                _merge_vat_analysis(out, _extract_vat_breakdown_from_xml_root(root))
                parsed_xml_found = True
                break
            except Exception:
                continue

    # fallback HTML parsing: try to find blocks/tables with labels
    if not out["total_amount"]:
        # try common labels
        lbl = soup.find(string=re.compile(r"Συνολική αξία|Συνολικό ποσό|Συνολικού ποσού", re.I))
        if lbl:
            parent = lbl.find_parent()
            if parent:
                nxt = parent.find_next(["strong","span","td","input"])
                if nxt:
                    out["total_amount"] = _clean_amount_to_comma(nxt.get("value") or nxt.get_text(" ", strip=True))
    if not out["issuer_vat"]:
        m_v = VAT_RE.search(html)
        if m_v: out["issuer_vat"] = m_v.group(0)
    if not out["issue_date"]:
        # look for ISO date in page text
        m = re.search(r"(\d{4}-\d{2}-\d{2})", html)
        if m:
            out["issue_date"] = _norm_date_to_ddmmyyyy(m.group(1))

    # table-structured fallback (common in vs.gr-like invoice pages)
    for table in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in table.find_all("tr")]
        rows = [r for r in rows if r]
        if len(rows) < 2:
            continue

        header = [h.strip().lower() for h in rows[0]]
        for data_row in rows[1:]:
            # column-mapped row when header and row have same column count
            if len(data_row) == len(header) and len(header) >= 2:
                mapped = {header[i]: data_row[i] for i in range(len(header))}

                if not out.get("issue_date"):
                    for hk, hv in mapped.items():
                        if "ημερομην" in hk or "date" in hk:
                            out["issue_date"] = _norm_date_to_ddmmyyyy(hv)
                            break

                if not out.get("doc_type"):
                    for hk, hv in mapped.items():
                        if "είδος" in hk or "παραστα" in hk or "type" in hk:
                            if hv and hv.strip():
                                out["doc_type"] = hv.strip()
                                break

            # key:value style rows
            if len(data_row) >= 2:
                key = (data_row[0] or "").strip().lower()
                val = (data_row[1] or "").strip()

                if not out.get("issue_date") and ("ημερομην" in key or "date" in key):
                    out["issue_date"] = _norm_date_to_ddmmyyyy(val)

                if "πληρωτ" in key or "payable" in key:
                    amount = _clean_amount_to_comma(val)
                    if amount:
                        out["total_amount"] = amount
                elif not out.get("total_amount") and ("σύνολο" in key or "συνολο" in key):
                    amount = _clean_amount_to_comma(val)
                    if amount:
                        out["total_amount"] = amount

                if not out.get("doc_type") and ("είδος" in key or "παραστα" in key or "type" in key):
                    if val:
                        out["doc_type"] = val

    # If doc_type still missing, try to find in page text or extracted doc_type
    if not out.get("doc_type"):
        # try to find "Είδος παραστατικού" label nearby
        lbl2 = soup.find(string=re.compile(r"Είδος παραστατικού", re.I))
        if lbl2:
            p = lbl2.find_parent()
            if p:
                nxt = p.find_next(["td","span","strong","input"])
                if nxt:
                    dt = (nxt.get("value") or nxt.get_text(" ", strip=True)).strip()
                    if dt:
                        out["doc_type"] = dt
    # mark is possibly already set

    # detect invoice wording in doc_type or anywhere in page
    if out.get("doc_type") and re.search(r"τιμολό?γιο|τιμολογιο", out["doc_type"], re.I):
        out["is_invoice"] = True
    else:
        # also detect numeric doc_type codes in doc_type (like 13.1 etc.) and treat according to rule:
        if out.get("doc_type"):
            mcode = re.search(r"(\d{1,2}\.\d{1,2})", str(out["doc_type"]))
            if mcode:
                code = mcode.group(1)
                # treat codes 13.1,13.2,13.31 as special (not-invoice) elsewhere — here we mark invoice if NOT those
                if code not in ("13.1", "13.2", "13.31"):
                    out["is_invoice"] = True
        # fallback page-wide detection of word 'τιμολόγ'
        page_txt = soup.get_text(" ", strip=True)
        if re.search(r"τιμολόγ", page_txt, flags=re.I):
            out["is_invoice"] = True

    # Normalize outputs
    if out["issuer_vat"]:
        m = VAT_RE.search(str(out["issuer_vat"]))
        out["issuer_vat"] = m.group(0) if m else re.sub(r"\D", "", str(out["issuer_vat"]))
    if out["issue_date"]:
        out["issue_date"] = _fmt_date_to_ddmmyyyy(out["issue_date"])
    if out["total_amount"]:
        out["total_amount"] = _clean_amount_to_comma(out["total_amount"])

    _merge_vat_analysis(out, _extract_vat_breakdown_from_html(soup, html))

    return out

def scrape_einvoice(url, timeout=15, debug=False):
    """
    ECOS e-invoicing pages: try to follow mydatalogo links or parse html, then attachments xml.
    Returns unified dict.
    """
    out = {"issuer_vat": None, "issue_date": None, "issuer_name": None,
           "progressive_aa": None, "doc_type": None, "total_amount": None,
           "is_invoice": False, "MARK": None, "source": "ECOS", "vat_analysis": None,
           "vat_analysis_inferred": False}
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        r = sess.get(url, timeout=timeout)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        if debug: print("einvoice fetch error:", e)
        return out
    soup = BeautifulSoup(html, "html.parser")
    # try mydatapi button detection
    mydatapi_candidate = None
    for a in soup.find_all("a", href=True):
        img = a.find("img")
        if img and img.get("src") and ("mydatlogo" in img["src"].lower() or "mydata" in img["src"].lower()):
            mydatapi_candidate = urljoin(r.url, a["href"])
            break
    if mydatapi_candidate:
        try:
            r2 = sess.get(mydatapi_candidate, timeout=timeout)
            r2.raise_for_status()
            resolved_url = r2.url
            # reuse mydatapi
            sub = scrape_mydatapi(resolved_url, timeout=timeout, debug=debug)
            sub["source"] = "ECOS->MyData"
            return sub
        except Exception:
            pass
    # else try attachments xml similar to wedoconnect
    candidate_urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(r.url, href)
        low = href.lower()
        if any(tok in low for tok in (".xml", "az-ubl", "ubl", "mydatafilecontainer", "mydata")):
            candidate_urls.append(full)
    seen=set()
    candidate_urls=[u for u in candidate_urls if not (u in seen or seen.add(u))]
    for cu in candidate_urls:
        try:
            r3 = sess.get(cu, timeout=timeout)
            r3.raise_for_status()
            content = r3.content
            text = r3.text
        except Exception:
            continue
        if "xml" in (r3.headers.get("Content-Type") or "").lower() or b"<?xml" in content[:200].lower() or re.search(r"<(Invoice|InvoicesDoc|cbc:Invoice)", text, flags=re.I):
            try:
                root = ET.fromstring(content)
            except Exception:
                try:
                    txt = content.decode("utf-8", errors="replace")
                    idx = txt.find("<?xml")
                    if idx != -1:
                        root = ET.fromstring(txt[idx:].encode("utf-8"))
                    else:
                        continue
                except Exception:
                    continue
            extracted = _extract_from_ubl_root(root)
            for k,v in extracted.items():
                if v and not out.get(k):
                    out[k]=v
            _merge_vat_analysis(out, _extract_vat_breakdown_from_xml_root(root))
            if out.get("total_amount") and out.get("issuer_vat"):
                break
    # fallback: HTML search
    if not out["issuer_vat"]:
        m = VAT_RE.search(html)
        if m: out["issuer_vat"]=m.group(0)
    if not out["total_amount"]:
        m = re.search(r"€\s*([0-9\.,]+)", html)
        if m: out["total_amount"]=_clean_amount_to_comma(m.group(1))
    _merge_vat_analysis(out, _extract_vat_breakdown_from_html(soup, html))
    if out.get("doc_type") and re.search(r"τιμολό?γιο|τιμολογιο", out["doc_type"], re.I):
        out["is_invoice"]=True
    return out

def scrape_impact(url, timeout=15, debug=False):
    """
    Impact (ECOS) view pages: 
    1) Αν υπάρχει #erpQrBtn, ακολούθησε το redirect (href / data-url / onclick / script).
    2) Αν ο τελικός προορισμός είναι mydatapi/mydata, τρέξε scrape_mydatapi και γύρνα το αποτέλεσμα.
    3) Fallback: παλιό HTML parsing των πεδίων μέσα στη σελίδα impact.
    """
    out = {
        "issuer_vat": None, "issue_date": None, "issuer_name": None,
        "progressive_aa": None, "doc_type": None, "total_amount": None,
        "is_invoice": False, "MARK": None, "source": "Impact", "vat_analysis": None,
        "vat_analysis_inferred": False
    }

    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        r = sess.get(url, timeout=timeout)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        if debug: print("impact fetch error:", e)
        return out

    soup = BeautifulSoup(html, "html.parser")

    # --- 1) Βρες URL του erpQrBtn (href, data-url, onclick, scripts) ---
    def _extract_erp_redirect(soup_obj, base_url):
        # άμεσο element
        btn = soup_obj.select_one("#erpQrBtn")
        cand = None
        if btn:
            # <a id="erpQrBtn" href="...">
            cand = btn.get("href")
            if not cand:
                # <button id="erpQrBtn" data-url="...">
                cand = btn.get("data-url") or btn.get("data-href")
            if not cand:
                # onclick="window.location.href='...';" / window.open("...")
                onclick = btn.get("onclick") or ""
                m = re.search(r"(?:location\.href|window\.location(?:\.href)?|document\.location(?:\.href)?|window\.open)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", onclick, flags=re.I)
                if m:
                    cand = m.group(1)
        # αν δεν βρέθηκε, δοκίμασε scripts που αναφέρουν το id
        if not cand:
            for s in soup_obj.find_all("script"):
                txt = s.string or s.get_text() or ""
                if "erpQrBtn" in txt.lower():
                    # πιάσε πρώτο URL μέσα στο script
                    m2 = re.search(r"https?://[^\s'\"<>]+", txt)
                    if m2:
                        cand = m2.group(0)
                        break
                    # ή ρυθμίσεις τύπου location.href='...'
                    m3 = re.search(r"(?:location\.href|window\.location(?:\.href)?|document\.location(?:\.href)?|window\.open)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", txt, flags=re.I)
                    if m3:
                        cand = m3.group(1)
                        break
        if cand:
            return urljoin(base_url, cand)
        return None

    erp_url = _extract_erp_redirect(soup, r.url)

    # Αν έχουμε erp_url, ακολούθησέ το και, αν πάει σε mydata, τρέξε scrape_mydatapi
    if erp_url:
        try:
            r2 = sess.get(erp_url, timeout=timeout, allow_redirects=True)
            r2.raise_for_status()
            final_u = r2.url.lower()
            if debug: print("erpQrBtn resolved to:", r2.url)

            if ("mydatapi.aade.gr" in final_u) or ("mydata.aade.gr" in final_u):
                sub = scrape_mydatapi(r2.url, timeout=timeout, debug=debug)
                sub["source"] = "Impact->MyData"
                return sub
            else:
                # μερικές φορές το intermediate redirect ξαναστέλνει σύνδεσμο για mydatapi στη σελίδα
                sub_try = scrape_mydatapi(r2.url, timeout=timeout, debug=debug)
                if any(sub_try.get(k) for k in ("issuer_vat", "issue_date", "total_amount", "MARK")):
                    sub_try["source"] = "Impact->MyData(?)"
                    return sub_try
        except Exception as e:
            if debug: print("erpQrBtn follow error:", e)
            # συνέχισε σε fallback

    # --- 2) Fallback: παλιό HTML parsing μέσα στην impact σελίδα ---
    # MARK (συνηθισμένο DOM για Impact)
    el = soup.select_one("span.field.field-Mark span.value, span.field-Mark span.value")
    if el and el.get_text(strip=True):
        out["MARK"] = el.get_text(strip=True)

    # Πίνακες με labels (ΑΦΜ, Ημερομηνία, Συνολική αξία, Είδος παραστατικού, Α/Α)
    for lbl in soup.find_all(string=re.compile(r"Α\.?Φ\.?Μ|Ημερομηνία|Συνολική αξία|Συνολικό ποσό|Είδος παραστατικού|Α/Α", re.I)):
        parent = lbl.find_parent()
        if not parent: continue
        nxt = parent.find_next(["input","td","span","strong"])
        if not nxt: continue
        val = nxt.get("value") or nxt.get_text(" ", strip=True)

        if re.search(r"Α\.?Φ\.?Μ", lbl, re.I) and val:
            m = re.search(r"(\d{9,})", val); 
            if m: out["issuer_vat"] = m.group(1)
        if re.search(r"Ημερομηνία", lbl, re.I) and val:
            out["issue_date"] = _norm_date_to_ddmmyyyy(val)
        if re.search(r"Συνολική αξία|Συνολικό ποσό", lbl, re.I) and val:
            out["total_amount"] = _clean_amount_to_comma(val)
        if re.search(r"Είδος παραστατικού", lbl, re.I) and val:
            out["doc_type"] = val
            if re.search(r"τιμολό?γιο|τιμολογιο", val, re.I):
                out["is_invoice"] = True

    # regex fallbacks
    if not out["issuer_vat"]:
        m = VAT_RE.search(html)
        if m: out["issuer_vat"] = m.group(0)
    if not out["total_amount"]:
        m = re.search(r"€\s*([0-9\.,]+)", html)
        if m: out["total_amount"] = _clean_amount_to_comma(m.group(1))

    _merge_vat_analysis(out, _extract_vat_breakdown_from_html(soup, html))

    return out


def scrape_epsilon(url, timeout=20, debug=False):
    """
    Getfile-only approach for Epsilon DocViewer links.
    ΔΕΧΕΤΑΙ και fd-links (π.χ. .../fd/8d885db2d8c14cbc320608de01edd6d3:97)
    και τα μετατρέπει αυτόματα σε DocViewer/UUID.
    
    Λειτουργεί επίσης για srv.parochos.gr που χρησιμοποιεί το ίδιο API.

    Επιστρέφει dict με:
      issuer_vat, issue_date (dd/mm/YYYY), issuer_name,
      progressive_aa, doc_type, total_amount, MARK, is_invoice (bool), tried_url
    """
    from urllib.parse import urlparse, parse_qs
    import re, requests
    from bs4 import BeautifulSoup
    import xml.etree.ElementTree as ET
    from datetime import datetime

    VAT_RE = re.compile(r"\b\d{9}\b")
    MARK_RE = re.compile(r"\b\d{15}\b")
    NON_INVOICE_CODES = {"11.1", "11.2", "13.31"}  # όχι τιμολόγια

    # --- ΝΕΟ: Κανονικοποίηση fd → DocViewer ---
    def _normalize_epsilon_url_to_docviewer(u: str) -> str:
        try:
            p = urlparse(u)
            # αν είναι ήδη DocViewer, μην πειράξεις τίποτα
            if "/DocViewer/" in p.path:
                return u
            # πιάσε το token μετά το /fd/
            m = re.search(r"/fd/([^/?#]+)", p.path, flags=re.I)
            if not m:
                return u
            token = m.group(1)
            # κόψε οτιδήποτε μετά από ':'
            token = token.split(":")[0]
            # κράτα μόνο hex
            hexonly = re.sub(r"[^0-9a-fA-F]", "", token)
            if len(hexonly) != 32:
                return u  # δεν είναι το αναμενόμενο format
            # βάλε παύλες: 8-4-4-4-12
            docid = f"{hexonly[0:8]}-{hexonly[8:12]}-{hexonly[12:16]}-{hexonly[16:20]}-{hexonly[20:32]}"
            new_path = f"/DocViewer/{docid}"
            return f"{p.scheme}://{p.netloc}{new_path}"
        except Exception:
            return u

    # εφαρμόζουμε την κανονικοποίηση
    url = _normalize_epsilon_url_to_docviewer(url)

    def _clean_amount_to_comma(raw):
        if raw is None: return None
        s = str(raw).strip()
        s = s.replace("€", "").replace("EUR", "").replace("\xa0", "").replace(" ", "")
        if '.' in s and ',' in s:
            if s.rfind(',') > s.rfind('.'):
                s = s.replace('.', '')
            else:
                s = s.replace(',', '')
                s = s.replace('.', ',')
        else:
            if '.' in s and ',' not in s:
                s = s.replace('.', ',')
        s = re.sub(r'[^\d,\-]', '', s)
        if s.count(',') > 1:
            parts = s.split(',')
            decimals = parts[-1]
            integer = ''.join(parts[:-1])
            s = integer + ',' + decimals
        return s or None

    def _fmt_date_to_ddmmyyyy(s):
        if not s: return None
        s = str(s).strip()
        try:
            dt = datetime.fromisoformat(s.split()[0])
            return dt.strftime("%d/%m/%Y")
        except Exception:
            pass
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y"):
            try:
                dt = datetime.strptime(s.split()[0], fmt)
                return dt.strftime("%d/%m/%Y")
            except Exception:
                continue
        m = re.search(r"(\d{1,2})[\/\-\.\s](\d{1,2})[\/\-\.\s](\d{4})", s)
        if m:
            d, mo, y = m.groups()
            return f"{int(d):02d}/{int(mo):02d}/{int(y)}"
        m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
        if m2:
            y, mo, d = m2.groups()
            return f"{int(d):02d}/{int(mo):02d}/{int(y)}"
        return None

    def _extract_from_ubl_root(root):
        out = {
            "issuer_vat": None,
            "issue_date": None,
            "issuer_name": None,
            "progressive_aa": None,
            "doc_type": None,
            "total_amount": None,
            "MARK": None,
        }
        for candidate_tag in ("AccountingSupplierParty", "SupplierParty", "Supplier", "AccountingSupplier"):
            for cand in root.findall(".//{*}" + candidate_tag):
                for el in cand.iter():
                    ln = el.tag.split("}")[-1].lower()
                    if ln in ("vatnumber", "companyid", "id"):
                        text = (el.text or "").strip()
                        m = VAT_RE.search(text)
                        if m:
                            out["issuer_vat"] = m.group(0); break
                if out["issuer_vat"]: break
            if out["issuer_vat"]: break
        if not out["issuer_vat"]:
            txt = ET.tostring(root, encoding="utf-8", method="text").decode("utf-8")
            m = VAT_RE.search(txt)
            if m: out["issuer_vat"] = m.group(0)
        for el in root.iter():
            ln = el.tag.split("}")[-1].lower()
            if ln in ("invoiceissuedate", "issuedate", "issue_date", "date", "documentdate", "issue", "invoice_date"):
                if el.text and el.text.strip():
                    out["issue_date"] = _fmt_date_to_ddmmyyyy(el.text.strip()); break
        for el in root.iter():
            ln = el.tag.split("}")[-1].lower()
            if ln in ("partyname", "name", "companyname", "suppliername", "legalname"):
                if el.text and el.text.strip():
                    out["issuer_name"] = el.text.strip(); break
        for el in root.iter():
            ln = el.tag.split("}")[-1].lower()
            if ln in ("aa", "a_a", "saa", "sequence", "sequentialid", "sequentialidnumeric"):
                txt = (el.text or "").strip()
                if txt and re.search(r"\d{1,}", txt):
                    out["progressive_aa"] = re.sub(r"\D", "", txt); break
        for el in root.iter():
            ln = el.tag.split("}")[-1].lower()
            if ln in ("invoicetype", "invoicetypecode", "invoicetypeid", "invoicetypecode"):
                if el.text and el.text.strip():
                    out["doc_type"] = el.text.strip(); break
        # Always prefer totalGrossValue if present
        found_total = False
        for el in root.iter():
            ln = el.tag.split("}")[-1].lower()
            if ln == "totalgrossvalue":
                txt = (el.text or "").strip()
                if txt and re.search(r"[0-9]", txt):
                    out["total_amount"] = _clean_amount_to_comma(txt)
                    found_total = True
                    break
        # If not found, try totalNetValue
        if not found_total:
            for el in root.iter():
                ln = el.tag.split("}")[-1].lower()
                if ln == "totalnetvalue":
                    txt = (el.text or "").strip()
                    if txt and re.search(r"[0-9]", txt):
                        out["total_amount"] = _clean_amount_to_comma(txt)
                        found_total = True
                        break
        # If still not found, try other common tags
        if not found_total:
            for el in root.iter():
                ln = el.tag.split("}")[-1].lower()
                if ln in ("payableamount", "legalmonetarytotal", "grandtotal", "totalamount", "amount", "payableamount"):
                    txt = (el.text or "").strip()
                    if txt and re.search(r"[0-9]", txt):
                        out["total_amount"] = _clean_amount_to_comma(txt)
                        found_total = True
                        break
        # Fallback: regex search in XML text
        if not out["total_amount"]:
            txt = ET.tostring(root, encoding="utf-8", method="text").decode("utf-8")
            m = re.search(r"([0-9]{1,3}(?:[.,][0-9]{3})*[.,][0-9]{1,2})", txt)
            if m: out["total_amount"] = _clean_amount_to_comma(m.group(1))
        for el in root.iter():
            ln = el.tag.split("}")[-1].lower()
            if "mark" in ln or "tmark" in ln:
                txt = (el.text or "").strip()
                mm = MARK_RE.search(txt)
                if mm:
                    out["MARK"] = mm.group(0); break
        return out

    # --- extract documentId από DocViewer URL / ή από query ?documentId= ---
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    docid = None
    if "/DocViewer/" in parsed.path:
        docid = parsed.path.split("/DocViewer/")[-1]
    else:
        q = parse_qs(parsed.query)
        if "documentId" in q:
            docid = q["documentId"][0]

    out = {
        "issuer_vat": None, "issue_date": None, "issuer_name": None,
        "progressive_aa": None, "doc_type": None, "total_amount": None,
        "MARK": None, "is_invoice": False, "tried_url": None,
        "vat_analysis": None,
        "vat_analysis_inferred": False,
        "source": "Epsilon-getfile-only" if "epsilon" in parsed.netloc.lower() else "Parochos-getfile-only"
    }

    if not docid:
        if debug: print("No documentId found in DocViewer URL.")
        return out

    getfile_url = f"{base}/filedocument/getfile?fileType=3&documentId={docid}"
    out["tried_url"] = getfile_url

    sess = requests.Session()
    sess.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": url})
    try:
        r = sess.get(getfile_url, timeout=timeout)
        r.raise_for_status()
    except Exception as e:
        if debug: print("getfile request failed:", e)
        return out

    content = r.content
    text = r.text or ""
    ctype = (r.headers.get("Content-Type") or "").lower()

    if "xml" in ctype or text.lstrip().startswith("<?xml") or re.search(r"<(Invoice|InvoicesDoc|cbc:Invoice|InvoiceLine)", text, flags=re.I):
        try:
            root = ET.fromstring(content)
            extracted = _extract_from_ubl_root(root)
            for k, v in extracted.items():
                if v: out[k] = v
            _merge_vat_analysis(out, _extract_vat_breakdown_from_xml_root(root))
            itype = out.get("doc_type")
            if itype and str(itype).strip() not in NON_INVOICE_CODES:
                out["is_invoice"] = True
            txt_all = ET.tostring(root, encoding="utf-8", method="text").decode("utf-8")
            if re.search(r"τιμολόγ", txt_all, flags=re.I):
                out["is_invoice"] = True
        except Exception as e:
            if debug: print("XML parse error:", e)
    else:
        try:
            soup = BeautifulSoup(text, "html.parser")
            id_map = {
                "vatnumber": "issuer_vat", "tdate": "issue_date",
                "tamount": "total_amount", "t_amount": "total_amount",
                "bname": "issuer_name", "saa": "progressive_aa",
                "aa": "progressive_aa", "s_aa": "progressive_aa",
                "snumber": "progressive_aa", "tmark": "MARK",
                "dtype": "doc_type", "invoiceType": "doc_type", "t_date": "issue_date",
            }
            for idn, field in id_map.items():
                el = soup.find(id=idn) or soup.find(attrs={"name": idn})
                if not el: continue
                val = (el.get("value") or "").strip() if el.name in ("input", "textarea") else el.get_text(" ", strip=True).strip()
                if not val: continue
                if field == "total_amount":
                    out[field] = out[field] or _clean_amount_to_comma(val)
                elif field == "issue_date":
                    out[field] = out[field] or _fmt_date_to_ddmmyyyy(val)
                elif field == "issuer_vat":
                    m = VAT_RE.search(val)
                    if m: out[field] = out[field] or m.group(0)
                elif field == "MARK":
                    m = MARK_RE.search(val)
                    if m: out[field] = out[field] or m.group(0)
                else:
                    out[field] = out[field] or val

            page_txt = soup.get_text(" ", strip=True)
            if not out["total_amount"]:
                m = re.search(r"(?:Συνολική αξία|Συνολικού ποσού|Συνολική αξία)[^\d\w\n\r]*([0-9\.,\s€]+)", page_txt, flags=re.I) or re.search(r"€\s*([0-9\.,]+)", page_txt)
                if m: out["total_amount"] = _clean_amount_to_comma(m.group(1))
            if not out["issuer_vat"]:
                mv = re.search(r"\b\d{9}\b", page_txt)
                if mv: out["issuer_vat"] = mv.group(0)
            if not out["issue_date"]:
                md = re.search(r"(\d{2}\/\d{2}\/\d{4})", page_txt)
                if md: out["issue_date"] = _fmt_date_to_ddmmyyyy(md.group(1))
            _merge_vat_analysis(out, _extract_vat_breakdown_from_html(soup, text))

            if out.get("doc_type") and str(out["doc_type"]).strip() not in NON_INVOICE_CODES:
                out["is_invoice"] = True
            if re.search(r"τιμολόγ", page_txt, flags=re.I):
                out["is_invoice"] = True
        except Exception as e:
            if debug: print("HTML parse error on getfile response:", e)

    if out["issuer_vat"]:
        m = VAT_RE.search(str(out["issuer_vat"]))
        out["issuer_vat"] = m.group(0) if m else re.sub(r"\D", "", str(out["issuer_vat"]))
    if out["issue_date"]:
        out["issue_date"] = _fmt_date_to_ddmmyyyy(out["issue_date"])
    if out["total_amount"]:
        out["total_amount"] = _clean_amount_to_comma(out["total_amount"])

    if not out["is_invoice"] and out.get("doc_type"):
        mcode = re.search(r"(\d{1,2}\.\d{1,2})", str(out["doc_type"]))
        if mcode and mcode.group(1) not in NON_INVOICE_CODES:
            out["is_invoice"] = True

    if debug:
        print("scrape_epsilon (getfile-only) result:", out)

    return out


def scrape_einvoicing_gr(url, timeout=15, debug=False):
    """
    e-Invoicing.gr (PEPPOL) pages, π.χ. https://e-invoicing.gr/edocuments/ViewInvoice?ct=PEPPOL&id=...&s=A&h=...
    1) Try the AADE/myDATA button or any embedded mydatapi link first and delegate to
       :func:`scrape_mydatapi` for those cases.
    2) Otherwise convert the ViewInvoice URL to an API endpoint and parse the response.
    """
    out = {
        "issuer_vat": None, "issue_date": None, "issuer_name": None,
        "progressive_aa": None, "doc_type": None, "total_amount": None,
        "is_invoice": False, "MARK": None, "source": "e-Invoicing.gr", "vat_analysis": None,
        "vat_analysis_inferred": False
    }

    def _clean_mark_candidate(raw_mark):
        txt = str(raw_mark or "").strip()
        if not txt:
            return None
        m = re.search(r"\b([0-9]{15})\b", txt)
        if m:
            return m.group(1)
        if txt.isdigit() and len(txt) == 15:
            return txt
        return None

    def _response_text(resp):
        raw = resp.content or b""
        head = raw[:4096].lower()
        if b"charset=utf-16" in head or b"charset=\"utf-16\"" in head:
            try:
                return raw.decode("utf-16")
            except Exception:
                pass
        if b"charset=utf-8" in head or b"charset=\"utf-8\"" in head:
            try:
                return raw.decode("utf-8", errors="replace")
            except Exception:
                pass
        try:
            return resp.text
        except Exception:
            try:
                return raw.decode(resp.apparent_encoding or "utf-8", errors="replace")
            except Exception:
                return raw.decode("utf-8", errors="replace")

    # early myDATA probe (page may include direct link or button)
    # Only apply when the URL does *not* already contain known API schemas.
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    has_legacy_params = all(k in qs and qs[k] for k in ("ct", "id", "s", "h"))
    has_peppol_params = all(k in qs and qs[k] for k in ("v", "ag", "c"))
    afm_hint = None
    if (not has_legacy_params) and (not has_peppol_params) and "/api/GetInvoice" not in parsed.path:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        try:
            r0 = sess.get(url, timeout=timeout, allow_redirects=True)
            r0.raise_for_status()
            html0 = _response_text(r0)

            myd = _extract_mydatapi_url_from_text(html0, r0.url)
            if not myd:
                soup0 = BeautifulSoup(html0, "html.parser")
                btn = soup0.find("span", class_=lambda c: c and "btn" in c,
                                 string=lambda s: s and "Παραστατικό" in s)
                if btn:
                    parent = btn.find_parent("a")
                    if parent:
                        href = parent.get("href") or ""
                        if href and href.strip() not in ("#", "javascript:void(0)"):
                            myd = urljoin(r0.url, href)
            if myd:
                if debug: print("e-invoicing.gr: delegating to mydatapi", myd)
                sub = scrape_mydatapi(myd, timeout=timeout, debug=debug)
                if isinstance(sub, dict):
                    mk = _clean_mark_candidate(sub.get("MARK"))
                    if mk:
                        sub["MARK"] = mk
                        return sub
                    afm_hint = (sub.get("issuer_vat") or afm_hint)
        except Exception as e:
            if debug: print("e-invoicing.gr early fetch error:", e)
    # continue below with parsed variable
    
    # Αν είναι ήδη API URL, χρησιμοποίησέ το
    if "/api/GetInvoice" in parsed.path:
        api_url = url
    else:
        # Μετατροπή ViewInvoice → API endpoint
        qs = parse_qs(parsed.query)
        base = f"{parsed.scheme}://{parsed.netloc}"
        ct = qs.get("ct", [""])[0]
        doc_id = qs.get("id", [""])[0]
        source = qs.get("s", [""])[0]
        hash_token = qs.get("h", [""])[0]
        vat_hint = qs.get("v", [""])[0]
        agent_hint = qs.get("ag", [""])[0]
        off_token = qs.get("c", [""])[0]

        if all([ct, doc_id, source, hash_token]):
            api_url = f"{base}/api/GetInvoice?contentType={ct}&id={doc_id}&source={source}&isPreview=True&hashToken={hash_token}"
        elif all([vat_hint, agent_hint, off_token]):
            params = {
                "contentType": "PEPPOL",
                "offTokenTp1": off_token,
                "vat": vat_hint,
                "agent": agent_hint,
                "isPreview": "True",
            }
            api_url = f"{base}/api/GetInvoice?{urlencode(params)}"
        else:
            if debug: print("e-invoicing.gr: missing required parameters")
            return out
    
    sess = requests.Session()
    sess.headers.update(HEADERS)
    
    try:
        r = sess.get(api_url, timeout=timeout)
        r.raise_for_status()
        html = _response_text(r)
    except Exception as e:
        if debug: print("e-invoicing.gr fetch error:", e)
        return out
    
    soup = BeautifulSoup(html, "html.parser")
    
    # 1) MARK - Αναζήτηση στο HTML
    mark = None

    # Pattern 1: Label-aware extraction γύρω από M.AR.K block
    idx = html.find("M.AR.K")
    if idx < 0:
        idx = html.upper().find("MARK:")
    if idx >= 0:
        mark = _clean_mark_candidate(html[idx:idx + 500])

    # Pattern 1b: DOM-based fallback
    if not mark:
        for row in soup.find_all("div", class_="row"):
            row_text = row.get_text(" ", strip=True)
            if "M.AR.K:" in row_text or "MARK:" in row_text:
                cols = row.find_all("div", class_=lambda c: c and "col" in c)
                if len(cols) >= 2:
                    mark = _clean_mark_candidate(cols[-1].get_text(strip=True))
                    if mark:
                        break

    # Pattern 2: Regex fallback για 15ψήφιο
    if not mark:
        m = MARK_RE.search(html)
        if m:
            mark = _clean_mark_candidate(m.group(0))

    out["MARK"] = mark
    
    # 2) ΑΦΜ Πελάτη - Αναζήτηση στο section "ΣΤΟΙΧΕΙΑ ΠΕΛΑΤΗ"
    counterpart_vat = None
    
    # Pattern 1: Βρες το section "ΣΤΟΙΧΕΙΑ ΠΕΛΑΤΗ" και ψάξε για ΑΦΜ
    customer_section = soup.find(string=re.compile(r"ΣΤΟΙΧΕΙΑ ΠΕΛΑΤΗ", re.I))
    if customer_section:
        parent = customer_section.find_parent("div", class_=lambda c: c and "backgrey" in c)
        if parent:
            for row in parent.find_all("div", class_="row"):
                row_text = row.get_text(" ", strip=True)
                if "Α.Φ.Μ" in row_text or "ΑΦΜ" in row_text:
                    cols = row.find_all("div", class_=lambda c: c and "col" in c)
                    if len(cols) >= 2:
                        afm_text = cols[-1].get_text(strip=True)
                        m = VAT_RE.search(afm_text)
                        if m:
                            counterpart_vat = m.group(0)
                            break
    
    # Pattern 2: Fallback - βρες όλα τα 9ψήφια
    if not counterpart_vat:
        all_vats = VAT_RE.findall(html)
        # Το πρώτο ΑΦΜ είναι συνήθως του εκδότη, το δεύτερο του πελάτη
        if len(all_vats) >= 2:
            counterpart_vat = all_vats[1]
        elif all_vats:
            counterpart_vat = all_vats[0]
    
    out["issuer_vat"] = counterpart_vat or afm_hint
    
    # 3) Ημερομηνία - Αναζήτηση "Ημ/νία έκδοσης"
    for div in soup.find_all("div", class_="fontSize8pt"):
        div_text = div.get_text(" ", strip=True)
        if re.search(r"Ημ/νία\s*έκδοσης", div_text, re.I):
            # Το pattern είναι: "Ημ/νία έκδοσης: 16-10-2025"
            m = re.search(r"Ημ/νία\s*έκδοσης:\s*(\d{2}-\d{2}-\d{4})", div_text, re.I)
            if m:
                out["issue_date"] = _fmt_date_to_ddmmyyyy(m.group(1))
                break
    
    # 4) Επωνυμία εκδότη
    supplier_section = soup.find("div", class_="BoldBlueHeader fontSize12pt")
    if supplier_section:
        out["issuer_name"] = supplier_section.get_text(strip=True)
    
    # 5) Αριθμός Παραστατικού (progressive_aa)
    for div in soup.find_all("div", class_="fontSize8pt"):
        div_text = div.get_text(" ", strip=True)
        if re.search(r"Αρ\.\s*Παραστατικού", div_text, re.I):
            # Παίρνει ΟΛΟ το κείμενο μετά το "Αρ. Παραστατικού:"
            m = re.search(r"Αρ\.\s*Παραστατικού:\s*(.*)", div_text, re.I)
            if m:
                out["progressive_aa"] = m.group(1).strip()
                break
    
    # 6) Είδος Παραστατικού
    doc_type_el = soup.find("div", class_="BoldBlueHeader fonSize10pt")
    if doc_type_el:
        doc_type_text = doc_type_el.get_text(strip=True)
        out["doc_type"] = doc_type_text
        if re.search(r"Τιμολόγιο", doc_type_text, re.I):
            out["is_invoice"] = True
    
    # 7) Συνολικό ποσό
    for row in soup.find_all("div", class_="row"):
        row_text = row.get_text(" ", strip=True)
        if re.search(r"ΤΕΛΙΚΟ ΠΟΣΟ|Συνολικ[όή].*ποσ[όo]", row_text, re.I):
            cols = row.find_all("div", class_=lambda c: c and "col" in c)
            if len(cols) >= 2:
                amount_text = cols[-1].get_text(strip=True)
                cleaned = _clean_amount_to_comma(amount_text)
                if cleaned:
                    out["total_amount"] = cleaned
                    break
    
    # Prefer explicit payable/final labels when present.
    # Use last match because e-invoicing pages can contain intermediate totals
    # before the final payable value.
    page_text = soup.get_text("\n", strip=True)
    total_priority_patterns = [
        r"Πληρωτ[έε]α\s*Αξ[ίι]α",
        r"Τελικ[όο]\s*Πληρωτ[έε]ο",
        r"Πληρωτ[έε]ο\s*Ποσ[όο]",
        r"Τελικ[ήη]\s*Αξ[ίι]α",
        r"ΤΕΛΙΚΟ\s*ΠΟΣΟ",
        r"Συνολικ[όή].*ποσ[όo]",
    ]
    for label_pat in total_priority_patterns:
        matches = list(re.finditer(rf"{label_pat}\s*[:\-]?\s*([0-9][0-9\.,]+)", page_text, re.I))
        if not matches:
            continue
        preferred = _clean_amount_to_comma(matches[-1].group(1))
        if preferred:
            out["total_amount"] = preferred
            break

    # Fallback για ποσό: ψάξε για pattern με EUR
    if not out["total_amount"]:
        m = re.search(r"([0-9\.,]+)\s*EUR", html)
        if m:
            out["total_amount"] = _clean_amount_to_comma(m.group(1))

    _merge_vat_analysis(out, _extract_vat_breakdown_from_html(soup, html))
    
    return out


def scrape_s1ecos(url, timeout=15, debug=False):
    """
    ECOS/S1 einvoice pages, π.χ. https://einvoice.s1ecos.gr/v/...
    1) Βρίσκουμε το #erpQrBtn και ακολουθούμε το redirect.
    2) Αν καταλήξει σε mydatapi/mydata → τρέχουμε scrape_mydatapi.
    3) Fallback: απλό parsing από τη σελίδα (αν υπάρχει).
    """
    out = {
        "issuer_vat": None, "issue_date": None, "issuer_name": None,
        "progressive_aa": None, "doc_type": None, "total_amount": None,
        "is_invoice": False, "MARK": None, "source": "S1ECOS", "vat_analysis": None,
        "vat_analysis_inferred": False
    }

    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        r = sess.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        if debug: print("s1ecos fetch error:", e)
        return out

    soup = BeautifulSoup(html, "html.parser")

    # --- εντοπισμός redirect από το κουμπί/σκριπτάκια του viewer ---
    def _extract_erp_redirect(soup_obj, base_url):
        btn = soup_obj.select_one("#erpQrBtn")
        cand = None
        if btn:
            cand = btn.get("href") or btn.get("data-url") or btn.get("data-href")
            if not cand:
                onclick = btn.get("onclick") or ""
                m = re.search(
                    r"(?:location\.href|window\.location(?:\.href)?|document\.location(?:\.href)?|window\.open)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
                    onclick, flags=re.I
                )
                if m: cand = m.group(1)

        if not cand:
            for s in soup_obj.find_all("script"):
                txt = s.string or s.get_text() or ""
                if "erpQrBtn" in txt.lower():
                    m2 = re.search(r"https?://[^\s'\"<>]+", txt)
                    if m2:
                        cand = m2.group(0); break
                    m3 = re.search(
                        r"(?:location\.href|window\.location(?:\.href)?|document\.location(?:\.href)?|window\.open)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
                        txt, flags=re.I
                    )
                    if m3:
                        cand = m3.group(1); break
        return urljoin(base_url, cand) if cand else None

    erp_url = _extract_erp_redirect(soup, r.url)

    if erp_url:
        try:
            r2 = sess.get(erp_url, timeout=timeout, allow_redirects=True)
            r2.raise_for_status()
            final_u = r2.url.lower()
            if debug: print("s1ecos erpQrBtn resolved to:", r2.url)

            if ("mydatapi.aade.gr" in final_u) or ("mydata.aade.gr" in final_u):
                sub = scrape_mydatapi(r2.url, timeout=timeout, debug=debug)
                sub["source"] = "S1ECOS->MyData"
                return sub
            else:
                # δοκίμασε απευθείας μήπως είναι ενδιάμεση σελίδα με embedded mydatapi στοιχεία
                sub_try = scrape_mydatapi(r2.url, timeout=timeout, debug=debug)
                if any(sub_try.get(k) for k in ("issuer_vat", "issue_date", "total_amount", "MARK")):
                    sub_try["source"] = "S1ECOS->MyData(?)"
                    return sub_try
        except Exception as e:
            if debug: print("s1ecos follow error:", e)
            # συνέχισε στο fallback

    # --- Fallback: ελαφρύ parsing στη σελίδα S1ECOS (αν έχει ευδιάκριτα labels/τιμές) ---
    el = soup.select_one("span.field.field-Mark span.value, span.field-Mark span.value")
    if el and el.get_text(strip=True):
        out["MARK"] = el.get_text(strip=True)

    for lbl in soup.find_all(string=re.compile(r"Α\.?Φ\.?Μ|Ημερομηνία|Συνολική αξία|Συνολικό ποσό|Είδος παραστατικού|Α/Α", re.I)):
        parent = lbl.find_parent()
        if not parent: continue
        nxt = parent.find_next(["input","td","span","strong"])
        if not nxt: continue
        val = nxt.get("value") or nxt.get_text(" ", strip=True)

        if re.search(r"Α\.?Φ\.?Μ", lbl, re.I) and val:
            m = re.search(r"(\d{9,})", val)
            if m: out["issuer_vat"] = m.group(1)
        if re.search(r"Ημερομηνία", lbl, re.I) and val:
            out["issue_date"] = _norm_date_to_ddmmyyyy(val)
        if re.search(r"Συνολική αξία|Συνολικό ποσό", lbl, re.I) and val:
            out["total_amount"] = _clean_amount_to_comma(val)
        if re.search(r"Είδος παραστατικού", lbl, re.I) and val:
            out["doc_type"] = val
            if re.search(r"τιμολό?γιο|τιμολογιο", val, re.I):
                out["is_invoice"] = True

    if not out["issuer_vat"]:
        m = VAT_RE.search(html)
        if m: out["issuer_vat"] = m.group(0)
    if not out["total_amount"]:
        m = re.search(r"€\s*([0-9\.,]+)", html)
        if m: out["total_amount"] = _clean_amount_to_comma(m.group(1))

    _merge_vat_analysis(out, _extract_vat_breakdown_from_html(soup, html))

    return out


def scrape_pegcloud(url, timeout=15, debug=False):
    """
    Pegcloud e-Invoicing scraper για receipts.
    Εξάγει: issuer_vat, issue_date, total_amount, MARK, κλπ.
    URLs: https://e-invoicing.pegcloud.io/pegasus/einv02/search_invoice01.php?auth_code=...
    """
    out = {
        "issuer_vat": None, "issue_date": None, "issuer_name": None,
        "progressive_aa": None, "doc_type": None, "total_amount": None,
        "is_invoice": False, "MARK": None, "source": "Pegcloud", "vat_analysis": None,
        "vat_analysis_inferred": False
    }
    
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        r = sess.get(url, timeout=timeout)
        r.raise_for_status()
        r.encoding = 'utf-8'
    except Exception as e:
        if debug: print(f"[Pegcloud RequestError] {e}")
        return out
    
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    
    # 1) MARK - Αναζήτηση "Μ.Αρ.Κ.:" ή "MARK"
    mark_match = re.search(r'(?:Μ\.Αρ\.Κ\.|MARK)\s*[:]\s*([0-9]{15})', html, re.I)
    if mark_match:
        out["MARK"] = mark_match.group(1)
    
    # Fallback: 15ψήφιο αριθμό
    if not out["MARK"]:
        m = re.search(r'\b([0-9]{15})\b', html)
        if m:
            out["MARK"] = m.group(1)
    
    # 2) Issue Date - αναζήτηση ημερομηνίας
    for pattern in [r"(?:Ημερομηνία|Issue Date)\s*[:]\s*([0-9/\-\.]+)", r"(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{4})"]:
        match = re.search(pattern, html, re.I)
        if match:
            out["issue_date"] = _norm_date_to_ddmmyyyy(match.group(1))
            break
    
    # 3) ΑΦΜ Εκδότη - Αναζήτηση "Α.Φ.Μ:" ή "VAT"
    vat_match = re.search(r'(?:Α\.?Φ\.?Μ|VAT)\s*[:]\s*([0-9]{9})', html, re.I)
    if vat_match:
        out["issuer_vat"] = vat_match.group(1)
    
    # Fallback: όλα τα 9ψήφια
    if not out["issuer_vat"]:
        all_vats = re.findall(r'\b([0-9]{9})\b', html)
        if all_vats:
            out["issuer_vat"] = all_vats[0]  # Πρώτο ΑΦΜ συνήθως εκδότη
    
    # 4) Total Amount - αναζήτηση χρηματικού ποσού
    total_match = re.search(r'(?:Σύνολο|Total|Ποσό)\s*[:]*\s*€?\s*([0-9\.,]+)', html, re.I)
    if total_match:
        out["total_amount"] = _clean_amount_to_comma(total_match.group(1))
    
    # Fallback: € pattern
    if not out["total_amount"]:
        m = re.search(r"€\s*([0-9\.,]+)", html)
        if m:
            out["total_amount"] = _clean_amount_to_comma(m.group(1))
    
    # 5) Doc Type - αναζήτηση είδους παραστατικού
    dtype_match = re.search(r'(?:Είδος|Type|Document)\s*[:]\s*([^\n<]+)', html, re.I)
    if dtype_match:
        out["doc_type"] = dtype_match.group(1).strip()
    
    # Detect invoice keyword
    if re.search(r"τιμολό?γιο|invoice", html, re.I):
        out["is_invoice"] = True

    _merge_vat_analysis(out, _extract_vat_breakdown_from_html(soup, html))
    
    return out


def scrape_megasoft(url, timeout=20, debug=False):
    """
    Megasoft InvoiceLink QR pages.
    Χρησιμοποιεί τεχνική τύπου scraper.py: προσθήκη peppol=true και parsing από rendered HTML labels.
    """
    out = {
        "issuer_vat": None, "issue_date": None, "issuer_name": None,
        "progressive_aa": None, "doc_type": None, "total_amount": None,
        "is_invoice": False, "MARK": None, "source": "Megasoft", "vat_analysis": None,
        "vat_analysis_inferred": False
    }

    sess = requests.Session()
    sess.headers.update(HEADERS)

    peppol_url = url
    if "peppol=true" not in url.lower():
        peppol_url = url + ("&" if "?" in url else "?") + "peppol=true"

    html = None
    last_err = None
    for candidate in [peppol_url, url]:
        try:
            r = sess.get(candidate, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            html_candidate = r.text
            # προτίμησε το peppol όταν έχει πραγματικό invoice content
            if ("peppol=true" in candidate.lower()) and re.search(r"Αναγνωριστικό\s*ΦΠΑ\s*Αγοραστή|ΒΤ-48|Είδος\s*Παραστατικού|Ημερομηνία\s*Έκδοσης|Σύνολο|Φ\.Π\.Α", html_candidate, re.I):
                html = html_candidate
                break
            if html is None:
                html = html_candidate
        except Exception as e:
            last_err = e

    if html is None:
        if debug:
            print("megasoft fetch error:", last_err)
        return out

    soup = BeautifulSoup(html, "html.parser")

    # Preferred path: follow "Προβολή μέσω myDATA" target and reuse myDATA scraper
    mydatapi_url = _extract_mydatapi_url_from_text(html, base_url=url)
    if not mydatapi_url:
        parsed = urlparse(url)
        qrcode_value = parse_qs(parsed.query).get("QrCode", [""])[0]
        candidate_targets = []
        button_ids = []

        # a[href]
        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            txt = (a.get_text(" ", strip=True) or "") + " " + href
            if "mydata" in txt.lower() or "timologioqr" in txt.lower() or "invoiceinspect/mydata" in txt.lower():
                candidate_targets.append(urljoin(url, href))

        # button-based discovery (e.g. "Προβολή μέσω MyData")
        for btn in soup.find_all("button"):
            btxt = btn.get_text(" ", strip=True) or ""
            if not re.search(r"mydata|timologioqr|προβολή\s*μέσω\s*mydata", btxt, re.I):
                continue
            bid = btn.get("id")
            if bid:
                button_ids.append(bid)

            formaction = btn.get("formaction")
            if formaction:
                candidate_targets.append(urljoin(url, formaction))

            form_id = btn.get("form")
            if form_id:
                form_el = soup.find("form", attrs={"id": form_id})
                if form_el and form_el.get("action"):
                    candidate_targets.append(urljoin(url, form_el.get("action")))

            parent_form = btn.find_parent("form")
            if parent_form and parent_form.get("action"):
                candidate_targets.append(urljoin(url, parent_form.get("action")))

            onclick = str(btn.get("onclick", ""))
            m_on = re.search(r"(?:location\.href\s*=|window\.open\s*\(|window\.location(?:\.href)?\s*=)\s*['\"]([^'\"]+)['\"]", onclick, re.I)
            if m_on:
                candidate_targets.append(urljoin(url, m_on.group(1)))

        # elements with data-url / data-href / onclick
        for el in soup.find_all(True):
            attrs = el.attrs or {}
            text_blob = " ".join([
                str(attrs.get("data-url", "")),
                str(attrs.get("data-href", "")),
                str(attrs.get("onclick", "")),
                el.get_text(" ", strip=True) if hasattr(el, "get_text") else "",
            ])
            if not re.search(r"mydata|timologioqr|invoiceinspect/mydata", text_blob, re.I):
                continue

            for attr_name in ("data-url", "data-href", "href"):
                val = attrs.get(attr_name)
                if val:
                    candidate_targets.append(urljoin(url, str(val)))

            onclick = str(attrs.get("onclick", ""))
            m = re.search(r"(?:location\.href|window\.open|window\.location(?:\.href)?)\s*\(\s*['\"]([^'\"]+)['\"]", onclick, re.I)
            if m:
                candidate_targets.append(urljoin(url, m.group(1)))

        # Search script handlers by button id / generic mydata urls
        for script in soup.find_all("script"):
            sc = script.string or script.get_text() or ""
            direct = _extract_mydatapi_url_from_text(sc, base_url=url)
            if direct:
                candidate_targets.append(direct)

            for m in re.finditer(r"/(?:invoiceinspect/mydata|invoiceinspect/qr)[^\s\"\'<>]*", sc, re.I):
                candidate_targets.append(urljoin(url, m.group(0)))

            for bid in button_ids:
                if bid and bid in sc:
                    m2 = re.search(r"(?:location\.href\s*=|window\.open\s*\(|window\.location(?:\.href)?\s*=)\s*['\"]([^'\"]+)['\"]", sc, re.I)
                    if m2:
                        candidate_targets.append(urljoin(url, m2.group(1)))

        # explicit known endpoint candidates with same QrCode
        if qrcode_value:
            q_enc = quote(qrcode_value, safe="")
            candidate_targets.extend([
                urljoin(url, f"/invoiceinspect/mydata?QrCode={q_enc}"),
                urljoin(url, f"/invoiceinspect/qr?QrCode={q_enc}&openMydata=true"),
                urljoin(url, f"/invoiceinspect/qr?QrCode={q_enc}&mydata=true"),
            ])

        seen = set()
        candidate_targets = [c for c in candidate_targets if c and not (c in seen or seen.add(c))]

        for candidate in candidate_targets:
            try:
                rr = sess.get(candidate, timeout=timeout, allow_redirects=True)
                rr.raise_for_status()
                mydatapi_url = (
                    _extract_mydatapi_url_from_text(rr.url, base_url=rr.url)
                    or _extract_mydatapi_url_from_text(rr.text, base_url=rr.url)
                )
                if mydatapi_url:
                    break
            except Exception:
                continue

    if mydatapi_url:
        if debug:
            print("megasoft resolved myDATA URL:", mydatapi_url)
        mydata_out = scrape_mydatapi(mydatapi_url, timeout=timeout, debug=debug)
        if isinstance(mydata_out, dict) and any(mydata_out.get(k) for k in ("MARK", "issuer_vat", "issue_date", "total_amount", "vat_analysis")):
            mydata_out["source"] = "Megasoft->MyData"
            _ensure_vat_analysis(mydata_out)
            return mydata_out

    # JS-aware fallback from scraper.py (real button click with Playwright)
    if not mydatapi_url:
        try:
            from scraper import _resolve_mydatapi_via_browser
            browser_url = _resolve_mydatapi_via_browser(url, timeout=timeout, debug=debug)
        except Exception:
            browser_url = None
        if browser_url:
            if debug:
                print("megasoft browser-resolved myDATA URL:", browser_url)
            mydata_out = scrape_mydatapi(browser_url, timeout=timeout, debug=debug)
            if isinstance(mydata_out, dict) and any(mydata_out.get(k) for k in ("MARK", "issuer_vat", "issue_date", "total_amount", "vat_analysis")):
                mydata_out["source"] = "Megasoft->MyData"
                _ensure_vat_analysis(mydata_out)
                return mydata_out

    # MARK
    m_mark = re.search(r"\b(\d{15})\b", html)
    if m_mark:
        out["MARK"] = m_mark.group(1)

    # issue_date
    m_date = re.search(r"Ημερομηνία\s*Έκδοσης[^\d]*(\d{1,2}[\/-]\d{1,2}[\/-]\d{4})", html, re.I)
    if not m_date:
        m_date = re.search(r"(\d{1,2}[\/-]\d{1,2}[\/-]\d{4})", html)
    if m_date:
        out["issue_date"] = _norm_date_to_ddmmyyyy(m_date.group(1))

    # doc type
    m_dtype = re.search(r"Είδος\s*Παραστατικού\s*</[^>]+>\s*<[^>]+>\s*([^<\n]+)", html, re.I)
    if m_dtype:
        out["doc_type"] = m_dtype.group(1).strip()
    else:
        dtype_match = re.search(r"(?:Είδος|Type|Document)\s*[:]\s*([^\n<]+)", html, re.I)
        if dtype_match:
            out["doc_type"] = dtype_match.group(1).strip()

    # VAT extraction by BT-48 (from scraper.py approach)
    for row in soup.find_all("tr"):
        row_text = row.get_text(" ", strip=True)
        if re.search(r"Αναγνωριστικό\s*ΦΠΑ\s*Αγοραστή.*ΒΤ-48", row_text, re.I):
            spans = row.find_all(["span", "td", "div"])
            for span in reversed(spans):
                txt = span.get_text(" ", strip=True)
                m_vat = re.search(r"(\d{9})", txt)
                if m_vat:
                    out["issuer_vat"] = m_vat.group(1)
                    break
            if out["issuer_vat"]:
                break

    if not out["issuer_vat"]:
        all_vats = re.findall(r"\b(\d{9})\b", html)
        if all_vats:
            out["issuer_vat"] = all_vats[0]

    # NOTE: intentionally no blind AFM fallback from decoded QrCode payload,
    # because this can return misleading AFM values.

    # total amount
    m_total = re.search(r"(?:ΠΛΗΡΩΤΕΟ\s*ΠΟΣΟ|Σύνολο|Total|Amount)[^\d\n]*([0-9][0-9\.,]+)", html, re.I)
    if m_total:
        out["total_amount"] = _clean_amount_to_comma(m_total.group(1))

    _merge_vat_analysis(out, _extract_vat_breakdown_from_html(soup, html))

    if out.get("doc_type") and re.search(r"τιμολό?γιο|τιμολογιο", out["doc_type"], re.I):
        out["is_invoice"] = True
    elif re.search(r"τιμολό?γιο|τιμολογιο", html, re.I):
        out["is_invoice"] = True

    return out


def _extract_mydatapi_link_from_page(html, base_url=None):
    if not html:
        return None
    mydatapi_url = _extract_mydatapi_url_from_text(html, base_url=base_url)
    if mydatapi_url:
        return mydatapi_url
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if "mydatapi.aade.gr" in href and "TimologioQR/QRInfo" in href:
            return urljoin(base_url or "", href)
    for btn in soup.find_all("button"):
        link = btn.find("a", href=True)
        if link:
            href = link["href"].strip()
            if "mydatapi.aade.gr" in href and "TimologioQR/QRInfo" in href:
                return urljoin(base_url or "", href)

    # dynamic viewers often keep the myDATA link inside onclick/script blocks
    for el in soup.find_all(True):
        onclick = str(el.get("onclick") or "")
        if not onclick:
            continue
        direct = _extract_mydatapi_url_from_text(onclick, base_url=base_url)
        if direct:
            return direct

    for sc in soup.find_all("script"):
        txt = sc.string or sc.get_text() or ""
        direct = _extract_mydatapi_url_from_text(txt, base_url=base_url)
        if direct:
            return direct
    return None


def _extract_dynamic_candidate_urls(html, base_url=None):
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    def _add(url_candidate):
        if not url_candidate:
            return
        c = str(url_candidate).strip().strip('"\'')
        if not c:
            return
        if c.startswith("javascript:") or c.startswith("mailto:"):
            return
        if c.startswith("/"):
            if not base_url:
                return
            c = urljoin(base_url, c)
        elif not re.match(r"^https?://", c, flags=re.I):
            if base_url:
                c = urljoin(base_url, c)
            else:
                return
        candidates.append(c)

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        txt = (a.get_text(" ", strip=True) or "")
        blob = f"{href} {txt}".lower()
        if re.search(r"mydata|timologioqr|qrinfo|requestdocs|xml|print|selection|raw", blob, re.I):
            _add(href)

    for iframe in soup.find_all("iframe", src=True):
        _add(iframe.get("src"))
    for emb in soup.find_all("embed", src=True):
        _add(emb.get("src"))

    onclick_patterns = [
        r"printPage\(\s*['\"]([^'\"]+)['\"]\s*\)",
        r"(?:window\.open|location\.href|window\.location(?:\.href)?)\s*\(?\s*['\"]([^'\"]+)['\"]",
        r"(?:url|href|src)\s*[:=]\s*['\"]([^'\"]+)['\"]",
    ]
    for el in soup.find_all(True):
        onclick = str(el.get("onclick") or "")
        if not onclick:
            continue
        for pat in onclick_patterns:
            for m in re.finditer(pat, onclick, re.I):
                _add(m.group(1))

    for sc in soup.find_all("script"):
        txt = sc.string or sc.get_text() or ""
        direct_myd = _extract_mydatapi_url_from_text(txt, base_url=base_url)
        if direct_myd:
            _add(direct_myd)
        for pat in onclick_patterns:
            for m in re.finditer(pat, txt, re.I):
                _add(m.group(1))

    seen = set()
    return [u for u in candidates if u and not (u in seen or seen.add(u))]


def _extract_primer_mark_from_url(url):
    parsed = urlparse(url)
    segment = parsed.path.rstrip("/").split("/")[-1]
    if re.fullmatch(r"\d{15}", segment):
        return segment
    m = re.search(r"/(\d{15})(?:[/?#]|$)", url)
    return m.group(1) if m else None


def scrape_iview(url, timeout=20, debug=False):
    out = {
        "issuer_vat": None, "issue_date": None, "issuer_name": None,
        "progressive_aa": None, "doc_type": None, "total_amount": None,
        "is_invoice": False, "MARK": None, "source": "IView", "vat_analysis": None,
        "vat_analysis_inferred": False
    }
    try:
        html = _fetch_url_text(url, timeout=timeout, debug=debug)
    except Exception as e:
        if debug:
            print("iview fetch error:", e)
        return out

    mydatapi_url = _extract_mydatapi_link_from_page(html, base_url=url)
    if mydatapi_url:
        if debug:
            print("iview resolved myDATA URL:", mydatapi_url)
        mydata_out = scrape_mydatapi(mydatapi_url, timeout=timeout, debug=debug)
        if isinstance(mydata_out, dict):
            out["MARK"] = _normalize_mark_value(mydata_out.get("MARK"))
            afm = (mydata_out.get("issuer_vat") or mydata_out.get("ΑΦΜ Πελάτη") or mydata_out.get("ΑΦΜ") or "").strip()
            out["issuer_vat"] = re.sub(r"\D", "", afm) if afm and afm != "N/A" else None
            out["doc_type"] = (mydata_out.get("doc_type") or mydata_out.get("Είδος Παραστατικού") or "").strip() or None
            out["issue_date"] = _norm_date_to_ddmmyyyy(mydata_out.get("issue_date") or mydata_out.get("Ημερομηνία") or mydata_out.get("Ημερομηνία Έκδοσης") or "") if (mydata_out.get("issue_date") or mydata_out.get("Ημερομηνία") or mydata_out.get("Ημερομηνία Έκδοσης")) else None
            out["total_amount"] = _clean_amount_to_comma(mydata_out.get("total_amount") or mydata_out.get("Συνολική αξία") or mydata_out.get("Συνολικό ποσό") or "") if (mydata_out.get("total_amount") or mydata_out.get("Συνολική αξία") or mydata_out.get("Συνολικό ποσό")) else None
            out["issuer_name"] = (mydata_out.get("issuer_name") or mydata_out.get("Επωνυμία") or "").strip() or None
            out["progressive_aa"] = (mydata_out.get("progressive_aa") or mydata_out.get("Α/Α") or mydata_out.get("ΑΑ") or "").strip() or None
            if isinstance(mydata_out.get("vat_analysis"), dict) and mydata_out.get("vat_analysis"):
                out["vat_analysis"] = mydata_out.get("vat_analysis")
                out["vat_analysis_inferred"] = bool(mydata_out.get("vat_analysis_inferred", False))
            else:
                _infer_vat_analysis_from_total(out)
            if out["doc_type"] and re.search(r"τιμολό?γιο|invoice", out["doc_type"], re.I):
                out["is_invoice"] = True
            out["source"] = "IView->MyData"
    _ensure_vat_analysis(out)
    return out


def scrape_primer(url, timeout=20, debug=False):
    out = {
        "issuer_vat": None, "issue_date": None, "issuer_name": None,
        "progressive_aa": None, "doc_type": None, "total_amount": None,
        "is_invoice": False, "MARK": None, "source": "Primer MyData", "vat_analysis": None,
        "vat_analysis_inferred": False
    }
    mark = _extract_primer_mark_from_url(url)
    if mark:
        out["MARK"] = mark
    try:
        html = _fetch_url_text(url, timeout=timeout, debug=debug)
    except Exception as e:
        if debug:
            print("primer fetch error:", e)
        return out

    def _fetch_primer_public_data(identifier):
        ident = str(identifier or "").strip()
        if not ident:
            return {}
        endpoints = [
            "https://mydata-backend.primer.gr/api/public/mydatasearch",
            "https://mydata.primer.gr/api/public/mydatasearch",
        ]
        payload = {"identifier": ident}
        last_err = None
        for ep in endpoints:
            try:
                rr = requests.post(ep, json=payload, timeout=max(10, timeout))
                rr.raise_for_status()
                data = rr.json()
                if str(data.get("status") or "") != "200":
                    continue
                return data.get("data") or {}
            except Exception as e:
                last_err = e
                continue
        if debug and last_err is not None:
            print("primer public API error:", last_err)
        return {}

    parsed_url = urlparse(url)
    last_seg = parsed_url.path.rstrip("/").split("/")[-1] if parsed_url.path else ""
    primer_identifiers = []
    if re.fullmatch(r"\d{15}", last_seg):
        primer_identifiers.append(last_seg)
    if re.fullmatch(r"[A-Fa-f0-9]{40}", last_seg):
        primer_identifiers.append(last_seg)
    if out.get("MARK"):
        primer_identifiers.append(str(out.get("MARK")))
    seen_ids = set()
    primer_identifiers = [x for x in primer_identifiers if x and not (x in seen_ids or seen_ids.add(x))]

    primer_data = {}
    for ident in primer_identifiers:
        primer_data = _fetch_primer_public_data(ident)
        if primer_data:
            break

    if isinstance(primer_data, dict) and primer_data:
        mark_val = primer_data.get("mark")
        normalized_mark = _normalize_mark_value(mark_val)
        if normalized_mark:
            out["MARK"] = normalized_mark

        issuer = primer_data.get("issuer") if isinstance(primer_data.get("issuer"), dict) else {}
        header = primer_data.get("invoiceHeader") if isinstance(primer_data.get("invoiceHeader"), dict) else {}
        summary = primer_data.get("invoiceSummary") if isinstance(primer_data.get("invoiceSummary"), dict) else {}

        out["issuer_vat"] = out.get("issuer_vat") or re.sub(r"\D", "", str(issuer.get("vatNumber") or "")) or None
        out["issuer_name"] = out.get("issuer_name") or _clean_issuer_name(issuer.get("companyName") or issuer.get("companySmallName"))
        out["issue_date"] = out.get("issue_date") or _norm_date_to_ddmmyyyy(header.get("issueDate") or "")
        out["progressive_aa"] = out.get("progressive_aa") or (str(header.get("aa") or "").strip() or None)
        out["doc_type"] = out.get("doc_type") or (str(header.get("invoiceType") or "").strip() or None)

        gross_total = summary.get("totalGrossValue")
        if gross_total is None:
            gross_total = primer_data.get("total")
        if gross_total is not None:
            out["total_amount"] = out.get("total_amount") or _clean_amount_to_comma(gross_total)

        vat_map = {}
        cat_to_rate = {
            "1": "24", "2": "13", "3": "6", "4": "17",
            "5": "9", "6": "4", "7": "0", "8": "0",
        }
        for row in (primer_data.get("invoiceDetails") or []):
            if not isinstance(row, dict):
                continue
            rate = row.get("vatPercent") or row.get("vatRate")
            if rate is None:
                rate = cat_to_rate.get(str(row.get("vatCategory") or "").strip())
            rate_key = _normalize_vat_rate_key(rate)
            if not rate_key:
                continue
            net_v = _amount_to_float(row.get("netValue"))
            vat_v = _amount_to_float(row.get("vatAmount"))
            gross_v = _amount_to_float(row.get("lineGrossValue"))
            if gross_v is None and net_v is not None and vat_v is not None:
                gross_v = net_v + vat_v
            bucket = vat_map.setdefault(rate_key, {"net": 0.0, "vat": 0.0, "gross": 0.0})
            if net_v is not None:
                bucket["net"] += net_v
            if vat_v is not None:
                bucket["vat"] += vat_v
            if gross_v is not None:
                bucket["gross"] += gross_v

        if vat_map:
            extracted = {
                k: {
                    "net_amount": _float_to_comma(v["net"]),
                    "vat_amount": _float_to_comma(v["vat"]),
                    "gross_amount": _float_to_comma(v["gross"]),
                }
                for k, v in vat_map.items()
            }
            extracted["__inferred__"] = False
            _merge_vat_analysis(out, extracted)
            _reconcile_single_rate_vat_with_total(out)

        if out.get("source") == "Primer MyData":
            out["source"] = "Primer Public API"

    def _render_with_browser(target_url):
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return None, None, [], target_url

        timeout_ms = int(max(timeout, 8) * 1000)
        found = {"url": None}
        captured_payloads = []
        final_url = target_url

        def _is_interesting_url(u):
            return bool(re.search(r"mydata|mydatapi|primer|timologioqr|xml|invoice|/api/", str(u or ""), re.I))

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()

                def _capture_request(req):
                    ru = req.url
                    if ("mydatapi.aade.gr" in ru or "mydata.aade.gr" in ru) and "TimologioQR/QRInfo" in ru:
                        found["url"] = ru

                def _capture_response(resp):
                    try:
                        ru = resp.url
                        if not _is_interesting_url(ru):
                            return
                        ctype = (resp.headers.get("content-type") or "").lower()
                        if not any(tok in ctype for tok in ("xml", "json", "text", "html")):
                            return
                        body = resp.text() or ""
                        if body:
                            captured_payloads.append(body)
                        if not found.get("url"):
                            maybe = _extract_mydatapi_url_from_text(body, base_url=ru)
                            if maybe:
                                found["url"] = maybe
                    except Exception:
                        return

                page.on("request", _capture_request)
                page.on("response", _capture_response)

                page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 18000))
                except Exception:
                    pass
                page.wait_for_timeout(3000)

                rendered_html = page.content()
                final_url = page.url
                myd_url = _extract_mydatapi_url_from_text(page.url, base_url=page.url) or \
                          _extract_mydatapi_url_from_text(rendered_html, base_url=page.url) or \
                          found.get("url")

                if not myd_url:
                    selectors = [
                        "a:has-text('MyData')",
                        "button:has-text('MyData')",
                        "a:has-text('myDATA')",
                        "button:has-text('myDATA')",
                        "a:has-text('Εκτύπωση')",
                        "button:has-text('Εκτύπωση')",
                        "a:has-text('Print')",
                        "button:has-text('Print')",
                        "a:has-text('XML')",
                        "button:has-text('XML')",
                    ]
                    for sel in selectors:
                        loc = page.locator(sel)
                        if loc.count() <= 0:
                            continue
                        try:
                            with page.expect_popup(timeout=3500) as popinfo:
                                loc.first.click(timeout=3500)
                            pop = popinfo.value
                            try:
                                pop.wait_for_load_state("domcontentloaded", timeout=7000)
                            except Exception:
                                pass
                            pop_html = pop.content()
                            if pop_html:
                                captured_payloads.append(pop_html)
                            maybe = _extract_mydatapi_url_from_text(pop.url, base_url=pop.url) or \
                                    _extract_mydatapi_url_from_text(pop_html, base_url=pop.url) or \
                                    found.get("url")
                            if maybe:
                                myd_url = maybe
                                break
                        except Exception:
                            try:
                                loc.first.click(timeout=3500)
                                page.wait_for_timeout(1500)
                                rendered_html = page.content()
                                final_url = page.url
                                if rendered_html:
                                    captured_payloads.append(rendered_html)
                                maybe = _extract_mydatapi_url_from_text(page.url, base_url=page.url) or \
                                        _extract_mydatapi_url_from_text(rendered_html, base_url=page.url) or \
                                        found.get("url")
                                if maybe:
                                    myd_url = maybe
                                    break
                            except Exception:
                                continue

                browser.close()
                return rendered_html, myd_url, captured_payloads, final_url
        except Exception as e:
            if debug:
                print("primer browser fallback error:", e)
        return None, None, captured_payloads, final_url

    def _extract_xml_and_urls(payload):
        if not payload:
            return None, []
        found_urls = []
        raw = str(payload)
        variants = [raw]
        unescaped = html_lib.unescape(str(payload))
        if unescaped != variants[0]:
            variants.append(unescaped)
        slash_unescaped = raw.replace("\\/", "/").replace("\\u002F", "/").replace("\\u003A", ":")
        if slash_unescaped not in variants:
            variants.append(slash_unescaped)
        slash_unescaped2 = unescaped.replace("\\/", "/").replace("\\u002F", "/").replace("\\u003A", ":")
        if slash_unescaped2 not in variants:
            variants.append(slash_unescaped2)

        for txt in variants:
            xml_candidate = _extract_xml_fragment(txt)
            if xml_candidate:
                return xml_candidate, []
            stripped = txt.lstrip()
            if stripped.startswith("<?xml") and re.search(r"<InvoicesDoc[\s\S]*?</InvoicesDoc>", stripped, re.I):
                return txt, []

            for m in re.finditer(r"https?://[^\s\"'<>]+", txt):
                raw_u = m.group(0).strip().strip('"\'(),;')
                u = raw_u.replace("&amp;", "&")
                low = u.lower()
                if "mydata.primer.gr" in low or "mydatapi.aade.gr" in low or "/download" in low or low.endswith(".xml"):
                    found_urls.append(u)

            # Some dynamic APIs return only UID, not a full URL.
            for m in re.finditer(r"\b([A-F0-9]{40})\b", txt):
                uid = m.group(1)
                found_urls.append(f"https://mydata.primer.gr/{uid}")

        seen_urls = set()
        return None, [u for u in found_urls if u and not (u in seen_urls or seen_urls.add(u))]

    mydatapi_url = _extract_mydatapi_link_from_page(html, base_url=url)

    # Dynamic Primer viewers can expose useful data only through secondary
    # "mydata selection" / print / raw-xml endpoints.
    secondary_payloads = []
    candidate_urls = _extract_dynamic_candidate_urls(html, base_url=url)
    sess = requests.Session()
    sess.headers.update(HEADERS)
    for cu in candidate_urls:
        try:
            rr = sess.get(cu, timeout=timeout, allow_redirects=True)
            rr.raise_for_status()
            rr.encoding = rr.apparent_encoding or "utf-8"
            payload = rr.text or ""
            secondary_payloads.append(payload)

            if not mydatapi_url:
                mydatapi_url = _extract_mydatapi_link_from_page(payload, base_url=rr.url) or \
                              _extract_mydatapi_url_from_text(payload, base_url=rr.url)
        except Exception:
            continue

    # JS-only Primer pages may expose myDATA links and XML only after runtime render.
    rendered_html, browser_myd, browser_payloads, browser_url = _render_with_browser(url)
    if browser_payloads:
        secondary_payloads.extend(browser_payloads)
    if rendered_html:
        secondary_payloads.append(rendered_html)
        for cu in _extract_dynamic_candidate_urls(rendered_html, base_url=browser_url or url):
            try:
                rr = sess.get(cu, timeout=timeout, allow_redirects=True)
                rr.raise_for_status()
                rr.encoding = rr.apparent_encoding or "utf-8"
                payload = rr.text or ""
                if payload:
                    secondary_payloads.append(payload)
                if not mydatapi_url:
                    mydatapi_url = _extract_mydatapi_link_from_page(payload, base_url=rr.url) or \
                                  _extract_mydatapi_url_from_text(payload, base_url=rr.url)
            except Exception:
                continue
    if not mydatapi_url and browser_myd:
        mydatapi_url = browser_myd

    if mydatapi_url:
        if debug:
            print("primer resolved myDATA URL:", mydatapi_url)
        mydata_out = scrape_mydatapi(mydatapi_url, timeout=timeout, debug=debug)
        if isinstance(mydata_out, dict):
            extracted_mark = _normalize_mark_value(mydata_out.get("MARK"))
            if extracted_mark:
                out["MARK"] = out["MARK"] or extracted_mark
            afm = (mydata_out.get("issuer_vat") or mydata_out.get("ΑΦΜ Πελάτη") or mydata_out.get("ΑΦΜ") or "").strip()
            out["issuer_vat"] = re.sub(r"\D", "", afm) if afm and afm != "N/A" else None
            out["doc_type"] = (mydata_out.get("doc_type") or mydata_out.get("Είδος Παραστατικού") or "").strip() or None
            out["issue_date"] = _norm_date_to_ddmmyyyy(mydata_out.get("issue_date") or mydata_out.get("Ημερομηνία") or mydata_out.get("Ημερομηνία Έκδοσης") or "") if (mydata_out.get("issue_date") or mydata_out.get("Ημερομηνία") or mydata_out.get("Ημερομηνία Έκδοσης")) else None
            out["total_amount"] = _clean_amount_to_comma(mydata_out.get("total_amount") or mydata_out.get("Συνολική αξία") or mydata_out.get("Συνολικό ποσό") or "") if (mydata_out.get("total_amount") or mydata_out.get("Συνολική αξία") or mydata_out.get("Συνολικό ποσό")) else None
            out["issuer_name"] = (mydata_out.get("issuer_name") or mydata_out.get("Επωνυμία") or "").strip() or None
            out["progressive_aa"] = (mydata_out.get("progressive_aa") or mydata_out.get("Α/Α") or mydata_out.get("ΑΑ") or "").strip() or None
            if out["doc_type"] and re.search(r"τιμολό?γιο|invoice", out["doc_type"], re.I):
                out["is_invoice"] = True
            if out.get("source") == "Primer MyData":
                out["source"] = "Primer MyData->MyData"

    xml_fragment = _extract_xml_fragment(html)
    if not xml_fragment:
        for payload in secondary_payloads:
            xml_fragment = _extract_xml_fragment(payload)
            if xml_fragment:
                break

    if not xml_fragment:
        for payload in [html] + secondary_payloads:
            t = (payload or "").lstrip()
            if t.startswith("<?xml") or re.search(r"<InvoicesDoc[\s\S]*?</InvoicesDoc>", t, re.I):
                xml_fragment = payload
                break

    # Follow dynamic links found inside payloads (e.g. downloadingInvoiceUrl)
    # and parse XML directly from those endpoints.
    if not xml_fragment:
        xml_fetch_urls = []
        for payload in [html] + secondary_payloads:
            maybe_xml, urls = _extract_xml_and_urls(payload)
            if maybe_xml:
                xml_fragment = maybe_xml
                break
            xml_fetch_urls.extend(urls)

        if not xml_fragment and xml_fetch_urls:
            seen_urls = set()
            queue = [u for u in xml_fetch_urls if u and not (u in seen_urls or seen_urls.add(u))]
            for cu in queue[:20]:
                try:
                    rr = sess.get(cu, timeout=timeout, allow_redirects=True)
                    rr.raise_for_status()
                    rr.encoding = rr.apparent_encoding or "utf-8"
                    payload = rr.text or ""
                    if payload:
                        secondary_payloads.append(payload)
                    maybe_xml, extra_urls = _extract_xml_and_urls(payload)
                    if maybe_xml:
                        xml_fragment = maybe_xml
                        break
                    for eu in extra_urls:
                        if eu not in seen_urls and len(queue) < 40:
                            seen_urls.add(eu)
                            queue.append(eu)
                except Exception:
                    continue

    if xml_fragment:
        try:
            root = ET.fromstring(xml_fragment.encode("utf-8"))
            extracted = _extract_from_ubl_root(root)
            for key, value in extracted.items():
                if value and not out.get(key):
                    out[key] = value
            extracted_mark = _extract_mark_from_primer_xml(root)
            if extracted_mark:
                out["MARK"] = out["MARK"] or extracted_mark

            seller_vat = None
            issuer = root.find('.//issuer') or root.find('.//{*}issuer')
            if issuer is not None:
                seller_el = issuer.find('.//vatNumber') or issuer.find('.//{*}vatNumber') or issuer.find('.//vatnumber')
                if seller_el is not None and seller_el.text:
                    m = VAT_RE.search(seller_el.text.strip())
                    if m:
                        seller_vat = m.group(0)

            afm = _extract_vat_from_primer_xml(root, seller_vat=seller_vat)
            if afm and not out.get("issuer_vat"):
                out["issuer_vat"] = afm

            # VAT analysis should come from XML invoiceDetails when available.
            _merge_vat_analysis(out, _extract_vat_breakdown_from_xml_root(root))
            _reconcile_single_rate_vat_with_total(out)
        except Exception as e:
            if debug:
                print("primer xml parse error:", e)

    if not out.get("issuer_vat"):
        soup = BeautifulSoup(html, "html.parser")
        for lbl in soup.find_all(string=re.compile(r"Α\.?Φ\.?Μ\.?", re.I)):
            parent = getattr(lbl, "parent", None)
            if not parent:
                continue
            text = parent.get_text(" ", strip=True)
            m = re.search(r"Α\.?Φ\.?Μ\.?[:\s]*([0-9]{9})", text, flags=re.I)
            if m:
                out["issuer_vat"] = m.group(1)
                break

    if not out.get("issuer_vat"):
        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text(" ", strip=True)
        m = re.search(r"\b([0-9]{9})\b", page_text)
        if m:
            out["issuer_vat"] = m.group(1)

    out["issuer_name"] = _clean_issuer_name(out.get("issuer_name"))
    _ensure_vat_analysis(out)
    return out


def scrape_eskap(url, timeout=20, debug=False):
    """
    ESKAP invoice pages.
    - Προσπαθεί πρώτα να εντοπίσει myDATA URL και να επαναχρησιμοποιήσει το scrape_mydatapi.
    - Χειρίζεται printPage('/invoice_old.php?...') fallback για σελίδα ανάλυσης.
    - Κάνει best-effort parsing από το HTML όταν δεν βρεθεί myDATA.
    """
    out = {
        "issuer_vat": None, "issue_date": None, "issuer_name": None,
        "progressive_aa": None, "doc_type": None, "total_amount": None,
        "is_invoice": False, "MARK": None, "source": "ESKAP", "vat_analysis": None,
        "vat_analysis_inferred": False
    }

    sess = requests.Session()
    sess.headers.update(HEADERS)

    try:
        r = sess.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        html = r.text
    except Exception as e:
        if debug:
            print("eskap fetch error:", e)
        return out

    soup = BeautifulSoup(html, "html.parser")

    def _apply_common_parse(target, html_text, soup_obj):
        if not target.get("MARK"):
            m_mark = MARK_RE.search(html_text)
            if m_mark:
                target["MARK"] = m_mark.group(0)

        if not target.get("issuer_vat"):
            vat_match = re.search(r'(?:Α\.?Φ\.?Μ|VAT)\s*[:]?\s*([0-9]{9})', html_text, re.I)
            if vat_match:
                target["issuer_vat"] = vat_match.group(1)
        if not target.get("issuer_vat"):
            all_vats = VAT_RE.findall(html_text)
            if all_vats:
                target["issuer_vat"] = all_vats[0]

        if not target.get("issue_date"):
            m_date = re.search(r"(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{4}|\d{4}[\/-]\d{2}[\/-]\d{2})", html_text)
            if m_date:
                target["issue_date"] = _norm_date_to_ddmmyyyy(m_date.group(1))

        if not target.get("total_amount"):
            m_total = re.search(r"(?:ΤΕΛΙΚΟ\s*ΠΟΣΟ|ΠΛΗΡΩΤΕΟ\s*ΠΟΣΟ|Σύνολο|Συνολική\s*αξία|Total|Amount)[^\d\n]*([0-9][0-9\.,]+)", html_text, re.I)
            if m_total:
                target["total_amount"] = _clean_amount_to_comma(m_total.group(1))
        if not target.get("total_amount"):
            m_eur = re.search(r"€\s*([0-9\.,]+)", html_text)
            if m_eur:
                target["total_amount"] = _clean_amount_to_comma(m_eur.group(1))

        if not target.get("doc_type"):
            dtype_match = re.search(r"(?:Είδος\s*Παραστατικού|Είδος|Type|Document)\s*[:]?\s*([^\n<]+)", html_text, re.I)
            if dtype_match:
                target["doc_type"] = dtype_match.group(1).strip()

        if not target.get("issuer_name"):
            for lbl in soup_obj.find_all(string=re.compile(r"Επωνυμία|Εκδότης|Supplier|Issuer", re.I)):
                parent = lbl.find_parent()
                if not parent:
                    continue
                nxt = parent.find_next(["input", "td", "span", "strong", "div"])
                if not nxt:
                    continue
                val = (nxt.get("value") or nxt.get_text(" ", strip=True) or "").strip()
                if val:
                    target["issuer_name"] = val
                    break

        if not target.get("progressive_aa"):
            m_aa = re.search(r"(?:Προοδευτικ(?:ός|ο)\s*α\/?α|Αρ\.?\s*Παραστατικού|Serial|No\.)\s*[:]?\s*([A-Za-z0-9\-_/]+)", html_text, re.I)
            if m_aa:
                target["progressive_aa"] = m_aa.group(1).strip()

        _merge_vat_analysis(target, _extract_vat_breakdown_from_html(soup_obj, html_text))

        if target.get("doc_type") and re.search(r"τιμολό?γιο|τιμολογιο|invoice", target["doc_type"], re.I):
            target["is_invoice"] = True
        elif re.search(r"τιμολό?γιο|τιμολογιο|invoice", html_text, re.I):
            target["is_invoice"] = True

    # 1) Προσπάθεια άμεσου εντοπισμού myDATA
    mydatapi_url = _extract_mydatapi_url_from_text(html, base_url=r.url)

    # 2) Συλλογή candidate urls από href/onclick (incl. printPage)
    candidate_urls = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text_blob = (a.get_text(" ", strip=True) or "") + " " + href
        if re.search(r"mydata|timologioqr|qrinfo|invoice_old", text_blob, re.I):
            candidate_urls.append(urljoin(r.url, href))

    for el in soup.find_all(True):
        onclick = str(el.get("onclick") or "")
        if not onclick:
            continue
        for m in re.finditer(r"printPage\(\s*['\"]([^'\"]+)['\"]\s*\)", onclick, re.I):
            candidate_urls.append(urljoin(r.url, m.group(1)))
        for m in re.finditer(r"(?:window\.open|location\.href|window\.location(?:\.href)?)\s*\(?\s*['\"]([^'\"]+)['\"]", onclick, re.I):
            candidate_urls.append(urljoin(r.url, m.group(1)))

    for script in soup.find_all("script"):
        sc = script.string or script.get_text() or ""
        direct = _extract_mydatapi_url_from_text(sc, base_url=r.url)
        if direct:
            candidate_urls.append(direct)
        for m in re.finditer(r"printPage\(\s*['\"]([^'\"]+)['\"]\s*\)", sc, re.I):
            candidate_urls.append(urljoin(r.url, m.group(1)))

    seen = set()
    candidate_urls = [u for u in candidate_urls if u and not (u in seen or seen.add(u))]

    # 3) Δοκίμασε candidates: βρες myDATA ή πάρε secondary html για fallback parsing
    secondary_html = None
    secondary_url = None
    for cu in candidate_urls:
        try:
            rr = sess.get(cu, timeout=timeout, allow_redirects=True)
            rr.raise_for_status()
            rr.encoding = rr.apparent_encoding or "utf-8"
        except Exception:
            continue

        resolved = _extract_mydatapi_url_from_text(rr.url, base_url=rr.url) or _extract_mydatapi_url_from_text(rr.text, base_url=rr.url)
        if resolved:
            mydatapi_url = resolved
            break

        if secondary_html is None and re.search(r"invoice_old|Ανάλυση\s*Φ\.?Π\.?Α|Συνολικ", rr.text, re.I):
            secondary_html = rr.text
            secondary_url = rr.url

    if mydatapi_url:
        if debug:
            print("eskap resolved myDATA URL:", mydatapi_url)
        mydata_out = scrape_mydatapi(mydatapi_url, timeout=timeout, debug=debug)
        if isinstance(mydata_out, dict) and any(mydata_out.get(k) for k in ("MARK", "issuer_vat", "issue_date", "total_amount", "vat_analysis")):
            mydata_out["source"] = "ESKAP->MyData"
            _ensure_vat_analysis(mydata_out)
            return mydata_out

    # fallback parsing από αρχικό + secondary (invoice_old)
    _apply_common_parse(out, html, soup)
    if secondary_html:
        if debug and secondary_url:
            print("eskap using secondary page for fallback parsing:", secondary_url)
        secondary_soup = BeautifulSoup(secondary_html, "html.parser")
        _apply_common_parse(out, secondary_html, secondary_soup)

    return out


def scrape_simpleinvoicing(url, timeout=20, debug=False):
    """
    SimpleInvoicing invoice pages.
    - Προσπαθεί πρώτα να εντοπίσει myDATA URL και να επαναχρησιμοποιήσει το scrape_mydatapi.
    - Χρησιμοποιεί headless-browser fallback για δυναμικά rendered πεδία.
    - Επιστρέφει πλήρες schema analysis με vat_analysis.
    """
    out = {
        "issuer_vat": None, "issue_date": None, "issuer_name": None,
        "progressive_aa": None, "doc_type": None, "total_amount": None,
        "is_invoice": False, "MARK": None, "series": None, "source": "SimpleInvoicing",
        "vat_analysis": None, "vat_analysis_inferred": False
    }

    sess = requests.Session()
    sess.headers.update(HEADERS)

    try:
        r = sess.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        html = r.text
    except Exception as e:
        if debug:
            print("simpleinvoicing fetch error:", e)
        return out

    soup = BeautifulSoup(html, "html.parser")

    def _apply_common_parse(target, html_text, soup_obj):
        page_text = soup_obj.get_text(" ", strip=True)

        # capture 'Σειρά' code which often hints at type (ΑΛΠ, ΤΔΠ, ΤΠΥ, ΤΠΠ, ΠΤ, κτλ.)
        if not target.get("series"):
            m_series = re.search(r"Σειρά\s*[:\-]?\s*([Α-ΩA-Z0-9]+)", page_text, re.I)
            if m_series:
                target["series"] = m_series.group(1).strip()

        # apply keyword-based refinements/heuristics
        _refine_doc_type(target, page_text)

        def _extract_amount_by_label(text, label_pattern):
            if not text:
                return None
            m = re.search(rf"{label_pattern}[\s:€]*([0-9][0-9\.,]+)", text, re.I)
            return m.group(1) if m else None

        if not target.get("MARK"):
            mark = _extract_input_or_text(soup_obj, "tmark", "mark", "mark_id", "markNumber")
            if mark:
                mm = MARK_RE.search(str(mark))
                target["MARK"] = mm.group(0) if mm else str(mark).strip()
        if not target.get("MARK"):
            m_mark = MARK_RE.search(page_text)
            if m_mark:
                target["MARK"] = m_mark.group(0)

        if not target.get("issuer_vat"):
            vat_raw = _extract_input_or_text(soup_obj, "vatnumber", "vat_number", "issuer_vat", "companyid", "vat")
            if not vat_raw:
                vat_match = re.search(r'(?:Α\.?Φ\.?Μ|VAT)\s*[:]?\s*([0-9]{9})', page_text, re.I)
                if vat_match:
                    vat_raw = vat_match.group(1)
            if not vat_raw:
                all_vats = VAT_RE.findall(page_text)
                if all_vats:
                    vat_raw = all_vats[0]
            if vat_raw:
                m = re.search(r"(\d{9,})", str(vat_raw))
                target["issuer_vat"] = (m.group(1)[:9] if m else re.sub(r"\D", "", str(vat_raw))[:9]) or None

        if not target.get("issue_date"):
            date_raw = _extract_input_or_text(soup_obj, "tdate", "t_date", "issueDate", "invoiceDate")
            if not date_raw:
                m_date = re.search(r"(\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{4}|\d{4}[\/-]\d{2}[\/-]\d{2})", page_text)
                if m_date:
                    date_raw = m_date.group(1)
            if date_raw:
                target["issue_date"] = _norm_date_to_ddmmyyyy(date_raw)

        if not target.get("total_amount"):
            total_raw = (
                _extract_amount_by_label(page_text, r"Τελικ[ήη]\s*Αξ[ίι]α")
                or _extract_amount_by_label(page_text, r"Πληρωτ[έε]ο\s*Ποσ[όο]")
                or _extract_amount_by_label(page_text, r"POS\s*/\s*e-?POS")
                or _extract_amount_by_label(page_text, r"Τρ[όο]ποι?\s*Πληρωμ[ήη]ς[\s\S]{0,120}")
            )
            if not total_raw:
                total_raw = _extract_input_or_text(soup_obj, "tamount", "t_amount", "totalAmount", "total_amount", "payableAmount")
            if not total_raw:
                m_total = re.search(r"(?:ΤΕΛΙΚΟ\s*ΠΟΣΟ|ΠΛΗΡΩΤΕΟ\s*ΠΟΣΟ|Τελικ[ήη]\s*Αξ[ίι]α|Total\s*Amount|Amount\s*Due)[^\d\n]*([0-9][0-9\.,]+)", page_text, re.I)
                if m_total:
                    total_raw = m_total.group(1)
            if not total_raw:
                m_eur = re.search(r"€\s*([0-9\.,]+)", page_text)
                if m_eur:
                    total_raw = m_eur.group(1)
            if total_raw:
                target["total_amount"] = _clean_amount_to_comma(total_raw)

        if not target.get("doc_type"):
            dt = _extract_input_or_text(soup_obj, "dtype", "doc_type", "documentType", "document_type")
            if not dt:
                m_dt = re.search(r"(?:Είδος\s*Παραστατικού|Είδος|Type|Document)\s*[:]?\s*([^\n<]+)", page_text, re.I)
                if m_dt:
                    dt = m_dt.group(1)
            if dt:
                cleaned_dt = re.sub(r"\s+", " ", str(dt)).strip(" :|\t\r\n")
                if cleaned_dt and cleaned_dt.lower() != "html>":
                    target["doc_type"] = cleaned_dt

        if not target.get("issuer_name"):
            iname = _extract_input_or_text(soup_obj, "bname", "issuer_name", "issuer", "companyName", "businessName")
            if iname:
                target["issuer_name"] = str(iname).strip()

        explicit_paa = None
        m_aa = re.search(r"(?:Προοδευτικ(?:ός|ο)\s*α\/?α|Αρ\.?\s*Παραστατικού|Α\s*\/\s*Α|A\s*\/\s*A|(?:\bΑΑ\b|\bAA\b)|Serial|No\.)\s*[:#]?\s*#?\s*([A-Za-z0-9\-_/]+)", page_text, re.I)
        if m_aa:
            explicit_paa = m_aa.group(1)
        if explicit_paa:
            target["progressive_aa"] = str(explicit_paa).strip()

        if not target.get("progressive_aa"):
            paa = _extract_input_or_text(soup_obj, "saa", "s_aa", "aa", "invoiceNo", "invoiceNumber", "serial")
            if paa:
                target["progressive_aa"] = str(paa).strip()

        # Explicit VAT summary extraction for layouts like:
        # ΑΝΑΛΥΣΗ ΦΠΑ -> 13% €4.20, and summary with Συν. Καθαρή Αξία / ΦΠΑ / Τελική Αξία
        explicit_vat = None
        net_sum_raw = _extract_amount_by_label(page_text, r"Συν\.?\s*Καθαρ[ήη]\s*Αξ[ίι]α")
        gross_sum_raw = _extract_amount_by_label(page_text, r"Τελικ[ήη]\s*Αξ[ίι]α") or target.get("total_amount")
        vat_sum_raw = None

        m_vat_label = re.search(r"Συν\.?\s*Καθαρ[ήη]\s*Αξ[ίι]α[\s\S]{0,120}?ΦΠΑ[\s:€]*([0-9][0-9\.,]+)", page_text, re.I)
        if m_vat_label:
            vat_sum_raw = m_vat_label.group(1)
        if not vat_sum_raw:
            m_vat_any = re.search(r"\bΦΠΑ\b[\s:€]*([0-9][0-9\.,]+)", page_text, re.I)
            if m_vat_any:
                vat_sum_raw = m_vat_any.group(1)

        m_rate_vat = re.search(r"ΑΝΑΛΥΣΗ\s*ΦΠΑ[\s\S]{0,220}?(\d{1,2}(?:[\.,]\d+)?)\s*%[\s:€]*([0-9][0-9\.,]+)?", page_text, re.I)
        if m_rate_vat:
            rate_raw = m_rate_vat.group(1)
            vat_from_section_raw = m_rate_vat.group(2)

            net_val = _amount_to_float(net_sum_raw)
            vat_val = _amount_to_float(vat_sum_raw or vat_from_section_raw)
            gross_val = _amount_to_float(gross_sum_raw)

            if gross_val is None and net_val is not None and vat_val is not None:
                gross_val = net_val + vat_val
            if net_val is None and gross_val is not None and vat_val is not None:
                net_val = gross_val - vat_val
            if vat_val is None and gross_val is not None and net_val is not None:
                vat_val = gross_val - net_val

            inferred = False
            if (net_val is None or vat_val is None) and gross_val is not None:
                try:
                    rate_num = float(str(rate_raw).replace(",", "."))
                except Exception:
                    rate_num = None
                if rate_num is not None and 0 <= rate_num <= 100:
                    denom = 1.0 + (rate_num / 100.0)
                    if denom > 0:
                        net_val = gross_val / denom
                        vat_val = gross_val - net_val
                        inferred = True

            rate_key = _normalize_vat_rate_key(rate_raw)
            if rate_key and (net_val is not None or vat_val is not None or gross_val is not None):
                explicit_vat = {
                    rate_key: {
                        "net_amount": _float_to_comma(net_val),
                        "vat_amount": _float_to_comma(vat_val),
                        "gross_amount": _float_to_comma(gross_val),
                    },
                    "__inferred__": inferred,
                }

        _merge_vat_analysis(target, _extract_vat_breakdown_from_html(soup_obj, html_text))
        if explicit_vat:
            _merge_vat_analysis(target, explicit_vat)
            if isinstance(target.get("vat_analysis"), dict):
                cleaned = {k: v for k, v in explicit_vat.items() if k != "__inferred__"}
                target["vat_analysis"] = cleaned
                target["vat_analysis_inferred"] = bool(explicit_vat.get("__inferred__", False))

        # if the page explicitly says it's a retail receipt/alp we should
        # never mark it as an invoice even if the word "invoice" appears
        # inside the branding string (e.g. "SimpleinvoiceproviderClient").
        if re.search(r"\b(?:απόδειξη|αποδειξη|αλπ)\b", page_text, re.I) or \
           (target.get("doc_type") and re.search(r"\b(?:απόδειξη|αποδειξη|αλπ)\b", str(target.get("doc_type")), re.I)):
            target["is_invoice"] = False
        # invoice detection: require word boundaries so that generic words like
        # "SimpleinvoiceproviderClient" do not trigger it.
        elif target.get("doc_type") and re.search(r"\b(?:τιμολό?γιο|τιμολογιο|invoice)\b", str(target.get("doc_type")), re.I):
            target["is_invoice"] = True
        elif re.search(r"\b(?:τιμολό?γιο|τιμολογιο|invoice)\b", page_text, re.I):
            target["is_invoice"] = True

    def _render_with_browser(target_url):
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return None, None
        timeout_ms = int(max(timeout, 8) * 1000)
        found = {"url": None}
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(ignore_https_errors=True)
                page = context.new_page()

                def _capture_request(req):
                    ru = req.url
                    if ("mydatapi.aade.gr" in ru or "mydata.aade.gr" in ru) and "TimologioQR/QRInfo" in ru:
                        found["url"] = ru

                page.on("request", _capture_request)
                page.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
                except Exception:
                    pass
                page.wait_for_timeout(2500)

                rendered_html = page.content()
                myd_url = _extract_mydatapi_url_from_text(page.url, base_url=page.url) or _extract_mydatapi_url_from_text(rendered_html, base_url=page.url) or found["url"]

                if not myd_url:
                    selectors = [
                        "a:has-text('MyData')",
                        "button:has-text('MyData')",
                        "a:has-text('myDATA')",
                        "button:has-text('myDATA')",
                    ]
                    for sel in selectors:
                        loc = page.locator(sel)
                        if loc.count() <= 0:
                            continue
                        try:
                            with page.expect_popup(timeout=3000) as popinfo:
                                loc.first.click(timeout=3000)
                            pop = popinfo.value
                            try:
                                pop.wait_for_load_state("domcontentloaded", timeout=6000)
                            except Exception:
                                pass
                            myd_url = _extract_mydatapi_url_from_text(pop.url, base_url=pop.url) or _extract_mydatapi_url_from_text(pop.content(), base_url=pop.url) or found["url"]
                            if myd_url:
                                break
                        except Exception:
                            try:
                                loc.first.click(timeout=3000)
                                page.wait_for_timeout(1200)
                                rendered_html = page.content()
                                myd_url = _extract_mydatapi_url_from_text(page.url, base_url=page.url) or _extract_mydatapi_url_from_text(rendered_html, base_url=page.url) or found["url"]
                                if myd_url:
                                    break
                            except Exception:
                                continue

                browser.close()
                return rendered_html, myd_url
        except Exception as e:
            if debug:
                print("simpleinvoicing browser fallback error:", e)
        return None, None

    def _fix_vat_analysis_consistency(target):
        if not isinstance(target, dict):
            return
        vmap = target.get("vat_analysis")
        if not isinstance(vmap, dict) or not vmap:
            return
        total_val = _amount_to_float(target.get("total_amount"))
        if total_val is None or total_val <= 0:
            return

        rows = [(k, v) for k, v in vmap.items() if k != "__inferred__" and isinstance(v, dict)]
        if len(rows) != 1:
            return

        rate_key, row = rows[0]
        gross_val = _amount_to_float(row.get("gross_amount"))
        if gross_val is None:
            net_val = _amount_to_float(row.get("net_amount"))
            vat_val = _amount_to_float(row.get("vat_amount"))
            if net_val is not None and vat_val is not None:
                gross_val = net_val + vat_val

        if gross_val is None:
            return

        if abs(gross_val - total_val) <= max(1.0, total_val * 0.10):
            return

        try:
            rate_num = float(str(rate_key).replace(",", "."))
        except Exception:
            return
        if rate_num < 0 or rate_num > 100:
            return

        denom = 1.0 + (rate_num / 100.0)
        if denom <= 0:
            return

        net_new = total_val / denom
        vat_new = total_val - net_new
        vmap[rate_key] = {
            "net_amount": _float_to_comma(net_new),
            "vat_amount": _float_to_comma(vat_new),
            "gross_amount": _float_to_comma(total_val),
        }
        target["vat_analysis_inferred"] = True

    mydatapi_url = _extract_mydatapi_url_from_text(html, base_url=r.url)

    candidate_urls = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        txt = (a.get_text(" ", strip=True) or "") + " " + href
        if re.search(r"mydata|timologioqr|qrinfo", txt, re.I):
            candidate_urls.append(urljoin(r.url, href))

    for el in soup.find_all(True):
        onclick = str(el.get("onclick") or "")
        if not onclick:
            continue
        for m in re.finditer(r"(?:window\.open|location\.href|window\.location(?:\.href)?)\s*\(?\s*['\"]([^'\"]+)['\"]", onclick, re.I):
            candidate_urls.append(urljoin(r.url, m.group(1)))

    for sc in soup.find_all("script"):
        txt = sc.string or sc.get_text() or ""
        direct = _extract_mydatapi_url_from_text(txt, base_url=r.url)
        if direct:
            candidate_urls.append(direct)

    seen = set()
    candidate_urls = [u for u in candidate_urls if u and not (u in seen or seen.add(u))]

    # Parse static page first so fallback fields (like progressive_aa)
    # are available even when returning data from MyData.
    _apply_common_parse(out, html, soup)

    for cu in candidate_urls:
        try:
            rr = sess.get(cu, timeout=timeout, allow_redirects=True)
            rr.raise_for_status()
            rr.encoding = rr.apparent_encoding or "utf-8"
        except Exception:
            continue
        resolved = _extract_mydatapi_url_from_text(rr.url, base_url=rr.url) or _extract_mydatapi_url_from_text(rr.text, base_url=rr.url)
        if resolved:
            mydatapi_url = resolved
            break

    if mydatapi_url:
        if debug:
            print("simpleinvoicing resolved myDATA URL:", mydatapi_url)
        mydata_out = scrape_mydatapi(mydatapi_url, timeout=timeout, debug=debug)
        if isinstance(mydata_out, dict) and any(mydata_out.get(k) for k in ("MARK", "issuer_vat", "issue_date", "total_amount", "vat_analysis")):
            for key in ("progressive_aa", "series", "doc_type", "issuer_name"):
                if (not mydata_out.get(key)) and out.get(key):
                    mydata_out[key] = out.get(key)
            if out.get("is_invoice") is not None:
                mydata_out["is_invoice"] = out.get("is_invoice")
            mydata_out["source"] = "SimpleInvoicing->MyData"
            _ensure_vat_analysis(mydata_out)
            return mydata_out

    rendered_html = None
    if not mydatapi_url:
        try:
            from scraper import _resolve_mydatapi_via_browser
            browser_url = _resolve_mydatapi_via_browser(url, timeout=timeout, debug=debug)
        except Exception:
            browser_url = None

        if browser_url:
            mydatapi_url = browser_url
        else:
            rendered_html, browser_myd_url = _render_with_browser(url)
            if browser_myd_url:
                mydatapi_url = browser_myd_url

    if mydatapi_url:
        if debug:
            print("simpleinvoicing browser-resolved myDATA URL:", mydatapi_url)
        mydata_out = scrape_mydatapi(mydatapi_url, timeout=timeout, debug=debug)
        if isinstance(mydata_out, dict) and any(mydata_out.get(k) for k in ("MARK", "issuer_vat", "issue_date", "total_amount", "vat_analysis")):
            for key in ("progressive_aa", "series", "doc_type", "issuer_name"):
                if (not mydata_out.get(key)) and out.get(key):
                    mydata_out[key] = out.get(key)
            if out.get("is_invoice") is not None:
                mydata_out["is_invoice"] = out.get("is_invoice")
            mydata_out["source"] = "SimpleInvoicing->MyData"
            _ensure_vat_analysis(mydata_out)
            return mydata_out

    _apply_common_parse(out, html, soup)
    if rendered_html:
        _apply_common_parse(out, rendered_html, BeautifulSoup(rendered_html, "html.parser"))

    # URL-based AA fallback as last resort only.
    if not out.get("progressive_aa"):
        for u in (url, r.url):
            try:
                token = urlparse(str(u)).path.split("/invoice/", 1)[1].split("/", 1)[0]
            except Exception:
                continue
            parts = [p.strip() for p in token.split("-") if p.strip()]
            numeric_parts = [p for p in parts if re.fullmatch(r"\d{1,10}", p)]
            if numeric_parts:
                out["progressive_aa"] = numeric_parts[0]
                break

    _fix_vat_analysis_consistency(out)

    summary_text = f"{html}\n{rendered_html or ''}"
    # final keyword/series based adjustment using the accumulated text
    _refine_doc_type(out, summary_text)
    if re.search(r"ΑΝΑΛΥΣΗ\s*ΦΠΑ", summary_text, re.I) and re.search(r"Τελικ[ήη]\s*Αξ[ίι]α", summary_text, re.I):
        va = out.get("vat_analysis")
        if isinstance(va, dict) and len([k for k in va.keys() if k != "__inferred__"]) >= 1:
            out["vat_analysis_inferred"] = False

    va = out.get("vat_analysis")
    if isinstance(va, dict):
        rows = [(k, v) for k, v in va.items() if k != "__inferred__" and isinstance(v, dict)]
        if len(rows) == 1:
            _, row = rows[0]
            net_v = _amount_to_float(row.get("net_amount"))
            vat_v = _amount_to_float(row.get("vat_amount"))
            gross_v = _amount_to_float(row.get("gross_amount"))
            total_v = _amount_to_float(out.get("total_amount"))
            if None not in (net_v, vat_v, gross_v, total_v):
                if abs((net_v + vat_v) - gross_v) <= 0.06 and abs(gross_v - total_v) <= 0.06:
                    out["vat_analysis_inferred"] = False

    _ensure_vat_analysis(out)
    return out

# ---------- classification helpers ----------

def _refine_doc_type(target, page_text):
    """Apply additional heuristics based on keywords, series codes and
    customer clues.

    This function is called repeatedly during parsing so it should be
    idempotent (not undo earlier conclusions).  It is used by the
    SimpleInvoicing scraper today but is written generically so other
    scrapers can call it in future.

    * `page_text`* is a flat text blob from the page (original or
      rendered).  `target` references the output dict being built.
    """

    # page_text may be None or empty; continue anyway since series
    # information can still drive classification
    text = page_text or ""

    # customer hints: if the text mentions "πελάτης λιανικής" or the
    # only VAT-like number is 999999999 / 000000000 treat as receipt
    if re.search(r"πελάτ[ηi]ς?\s+λιανικ[ήη]ς", text, re.I) or re.search(r"\b(?:9{9}|0{9})\b", text):
        target["is_invoice"] = False
        # we still keep doc_type/series for later reference

    # map common series/doc codes to human-readable hints
    series = target.get("series")
    if series:
        canon = series.upper()
        mapping = {
            "ΑΛΠ": "Απόδειξη λιανικής πώλησης",
            "ΑΠΥ": "Απόδειξη παροχής υπηρεσιών",
            "ΤΔΠ": "Τιμολόγιο/Δελτίο αποστολής",
            "ΤΔΑ": "Τιμολόγιο δελτίο αποστολής",
            "ΤΠΥ": "Τιμολόγιο παροχής υπηρεσιών",
            "ΤΠ": "Τιμολόγιο πώλησης",
            "ΤΠΠ": "Τιμολόγιο πωλήσεων",
            "ΠΤ": "Πιστωτικό τιμολόγιο",
        }
        if canon in mapping:
            target["doc_type"] = mapping[canon]
        # series starting with 'Α' often indicate receipt
        if canon.startswith("Α"):
            target["is_invoice"] = False
        elif canon.startswith("Τ") or canon.startswith("Π"):
            target["is_invoice"] = True

    # Also normalize doc_type when it already contains short codes
    dt_code = re.sub(r"\s+", "", str(target.get("doc_type") or "")).upper()
    dt_map = {
        "ΑΛΠ": "Απόδειξη λιανικής πώλησης",
        "ΑΠΥ": "Απόδειξη παροχής υπηρεσιών",
        "ΤΠΥ": "Τιμολόγιο παροχής υπηρεσιών",
        "ΤΔΑ": "Τιμολόγιο δελτίο αποστολής",
        "ΤΠ": "Τιμολόγιο πώλησης",
        "ΠΤ": "Πιστωτικό τιμολόγιο",
    }
    if dt_code in dt_map:
        target["doc_type"] = dt_map[dt_code]
        if dt_code in ("ΑΛΠ", "ΑΠΥ"):
            target["is_invoice"] = False
        else:
            target["is_invoice"] = True

    # look for explicit document keywords after series logic so we can
    # override the simple series->invoice mapping
    # receipt keywords (beyond the generic "απόδειξη" we already use)
    receipt_terms = [r"απόδειξη παροχής υπηρεσιών", r"αλπ"]
    invoice_terms = [
        r"τιμολόγιο\s*δελτίο\s*αποστολής",
        r"τιμολόγιο\s*παροχής\s*υπηρεσιών",
        r"τιμολόγιο\s*πωλήσεων",
        r"πιστωτικό\s*τιμολόγιο",
    ]
    for pat in receipt_terms:
        if re.search(pat, text, re.I):
            target["is_invoice"] = False
            break
    else:
        for pat in invoice_terms:
            if re.search(pat, text, re.I):
                target["is_invoice"] = True
                break

# ---------- entry point demonstration ----------
def detect_and_scrape(url, timeout=20, debug=False):
    """
    Convenience wrapper: detect source from URL and call appropriate scraper.
    """
    domain = urlparse(url).netloc.lower()
    if "www1.aade.gr" in domain:
        result = scrape_www1_aade(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "mydatapi.aade.gr" in domain or "mydata.aade.gr" in domain:
        result = scrape_mydatapi(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "wedoconnect" in domain:
        result = scrape_wedoconnect(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "einvoice.s1ecos.gr" in domain:
        result = scrape_s1ecos(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "impact.gr" in domain or "einvoice.impact" in domain:
        result = scrape_impact(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "epsilonnet.gr" in domain or "epsilon" in domain:
        result = scrape_epsilon(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "parochos.gr" in domain:
        result = scrape_epsilon(url, timeout=timeout, debug=debug)  # Χρησιμοποιεί το ίδιο API
        return _finalize_detect_result(result)
    if "mydata.primer.gr" in domain or "primer.gr" in domain:
        result = scrape_primer(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "iview.gr" in domain:
        result = scrape_iview(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "pegcloud.io" in domain or "pegcloud" in domain:
        result = scrape_pegcloud(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "e-invoicing.gr" in domain:
        result = scrape_einvoicing_gr(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "megasoft" in domain or "invoicelink" in domain:
        result = scrape_megasoft(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "simpleinvoicing.gr" in domain or "simpleinvoicing" in domain:
        result = scrape_simpleinvoicing(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    if "eskap.gr" in domain or "eskap" in domain:
        result = scrape_eskap(url, timeout=timeout, debug=debug)
        return _finalize_detect_result(result)
    # fallback: attempt generic wedoconnect-like scraping then page scanning
    result = scrape_wedoconnect(url, timeout=timeout, debug=debug)
    return _finalize_detect_result(result)

# if run as script, quick demo input
if __name__ == "__main__":
    u = input("URL: ").strip()
    u = _normalize_url(u)
    res = detect_and_scrape(u, debug=True)
    import pprint
    pprint.pprint(res)
