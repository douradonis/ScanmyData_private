# fetch.py
import requests
import pandas as pd
from collections import defaultdict
from datetime import datetime
from dateutil.relativedelta import relativedelta
from dateutil.parser import parse
from typing import Tuple, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import re

try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET

from activity_monitor import monitor_resources

def _safe_strip(s):
    return str(s).strip() if s else ""

def find_in_element_by_localnames(elem, localnames):
    if elem is None:
        return ""
    for sub in elem.iter():
        tag = sub.tag
        lname = tag.split("}", 1)[1] if "}" in tag else tag
        if lname in localnames:
            txt = _safe_strip(sub.text)
            if txt:
                return txt
    return ""

def extract_issuer_info(invoice_elem, ns):
    vat = ""
    name = ""
    issuer = invoice_elem.find("ns:issuer", ns)
    if issuer is not None:
        vat = _safe_strip(issuer.findtext("ns:vatNumber", default="", namespaces=ns))
        if not vat:
            vat = find_in_element_by_localnames(issuer, ["vatNumber", "VATNumber", "vatnumber"])
        name = _safe_strip(issuer.findtext("ns:name", default="", namespaces=ns))
        if not name:
            name = find_in_element_by_localnames(issuer, ["name", "Name", "companyName", "partyName", "partyType", "party"])
    else:
        vat = find_in_element_by_localnames(invoice_elem, ["vatNumber", "VATNumber", "vatnumber"])
        name = find_in_element_by_localnames(invoice_elem, ["name", "Name", "companyName", "partyName", "partyType", "party"])
    return _safe_strip(vat), _safe_strip(name)

def to_float_safe(x):
    try:
        return float(x)
    except Exception:
        try:
            return float(str(x).strip().replace(",", "."))
        except Exception:
            return 0.0

def format_date_to_ddmmyyyy(value: str) -> str:
    if not value:
        return ""
    v = str(value).strip()
    if "/" in v and len(v.split("/")[0]) <= 2:
        return v
    try:
        dt = parse(v)
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return v

def format_decimal_comma(value) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, (int, float)):
            s = f"{value:.2f}"
            return s.replace(".", ",")
        vs = str(value).strip()
        num = float(vs.replace(",", "."))
        s = f"{num:.2f}"
        return s.replace(".", ",")
    except Exception:
        return str(value).strip()


def _extract_pagination_cursors(root):
    """Return pagination cursors from an AADE XML response.

    Prefer the documented RequestDocs cursor pair (nextPartitionKey/nextRowKey),
    but also support legacy/non-documented nextPartitionToken responses that some
    environments appear to return.
    """
    cursors = {
        "nextPartitionKey": "",
        "nextRowKey": "",
        "nextPartitionToken": "",
    }
    for elem in root.iter():
        tag = elem.tag
        lname = tag.split("}", 1)[-1] if "}" in tag else tag
        text = _safe_strip(elem.text)
        if not text:
            continue
        if lname in cursors and not cursors[lname]:
            cursors[lname] = text
    return cursors


def _doc_signature(row: dict) -> tuple:
    return (
        _safe_strip(row.get("mark")),
        _safe_strip(row.get("issueDate")),
        _safe_strip(row.get("series")),
        _safe_strip(row.get("aa")),
        _safe_strip(row.get("type")),
        _safe_strip(row.get("vatCategory")),
        str(row.get("totalNetValue")),
        str(row.get("totalVatAmount")),
        str(row.get("totalValue")),
    )

def _fetch_request_docs(mark: str, date_from: str, date_to: str, aade_user: str, aade_key: str, debug: bool = False) -> Tuple[List[dict], set]:
    """Fetch RequestDocs and return rows with transmitted marks."""
    URL_REQUEST_DOCS = "https://mydatapi.aade.gr/myDATA/RequestDocs"
    headers = {"aade-user-id": aade_user, "Ocp-Apim-Subscription-Key": aade_key}
    all_rows = []
    seen_signatures = set()
    current_mark = _safe_strip(mark) or "0"
    params_docs = {"mark": current_mark, "dateFrom": date_from, "dateTo": date_to}
    ns = {'ns': 'http://www.aade.gr/myDATA/invoice/v1.0'}
    resume_guard_marks = set()

    while True:
        resp = requests.get(URL_REQUEST_DOCS, params=params_docs, headers=headers)
        if debug: print(f"[RequestDocs] Status: {resp.status_code}")
        if resp.status_code != 200:
            raise RuntimeError(f"RequestDocs HTTP {resp.status_code}: {(resp.text or '')[:1000]}")

        root = ET.fromstring(resp.content)
        page_rows = []
        page_max_mark = current_mark
        for invoice in root.findall(".//ns:invoice", ns):
            mark_val = _safe_strip(invoice.findtext("ns:mark", default="", namespaces=ns))
            header = invoice.find("ns:invoiceHeader", ns)
            issueDate_raw = _safe_strip(header.findtext("ns:issueDate", default="", namespaces=ns)) if header is not None else ""
            issueDate = format_date_to_ddmmyyyy(issueDate_raw)
            series = _safe_strip(header.findtext("ns:series", default="", namespaces=ns)) if header is not None else ""
            aa = _safe_strip(header.findtext("ns:aa", default="", namespaces=ns)) if header is not None else ""

            invoice_type = ""
            if header is not None:
                invoice_type = _safe_strip(header.findtext("ns:invoiceType", default="", namespaces=ns))
                if not invoice_type:
                    invoice_type = find_in_element_by_localnames(header, ["invoiceType", "InvoiceType", "invoiceCategory", "type", "documentType"])
            if not invoice_type:
                invoice_type = find_in_element_by_localnames(invoice, ["invoiceType", "InvoiceType", "invoiceCategory", "type", "documentType"])

            vatissuer, Name_issuer = extract_issuer_info(invoice, ns)

            # Extract paymentMethodDetails type
            payment_method_type = ""
            payment_methods = invoice.findall(".//ns:paymentMethods/ns:paymentMethodDetails", ns)
            if not payment_methods:
                payment_methods = invoice.findall(".//paymentMethods/paymentMethodDetails")
            if payment_methods:
                first_payment = payment_methods[0]
                payment_method_type = _safe_strip(
                    first_payment.findtext("ns:type", default="", namespaces=ns) or
                    first_payment.findtext("type", default="") or ""
                )

            vat_groups = defaultdict(lambda: {"netValue": 0.0, "vatAmount": 0.0})
            details_nodes = invoice.findall(".//ns:invoiceDetails", ns) or invoice.findall(".//ns:invoiceDetail", ns) or invoice.findall(".//invoiceDetails") or invoice.findall(".//invoiceDetail")
            for detail in details_nodes:
                net = to_float_safe(detail.findtext("ns:netValue", default=None, namespaces=ns) or detail.findtext("netValue") or detail.findtext("NetValue") or 0)
                vat = to_float_safe(detail.findtext("ns:vatAmount", default=None, namespaces=ns) or detail.findtext("vatAmount") or detail.findtext("VatAmount") or 0)
                cat = _safe_strip(detail.findtext("ns:vatCategory", default=None, namespaces=ns) or detail.findtext("vatCategory") or detail.findtext("VatCategory") or "1")
                vat_groups[cat]["netValue"] += net
                vat_groups[cat]["vatAmount"] += vat

            if not vat_groups:
                summary_node = invoice.find("ns:invoiceSummary", ns) or invoice.find("invoiceSummary")
                if summary_node is not None:
                    net = to_float_safe(summary_node.findtext("ns:totalNetValue", default="0", namespaces=ns) or summary_node.findtext("totalNetValue") or "0")
                    vat = to_float_safe(summary_node.findtext("ns:totalVatAmount", default="0", namespaces=ns) or summary_node.findtext("totalVatAmount") or "0")
                    vat_groups["1"]["netValue"] += net
                    vat_groups["1"]["vatAmount"] += vat

            for vat_cat, totals in vat_groups.items():
                total_value = round(totals["netValue"] + totals["vatAmount"], 2)
                row = {
                    "mark": mark_val,
                    "issueDate": issueDate,
                    "series": series,
                    "aa": aa,
                    "AA": aa,
                    "type": invoice_type,
                    "vatCategory": vat_cat,
                    "totalNetValue": round(totals["netValue"], 2),
                    "totalVatAmount": round(totals["vatAmount"], 2),
                    "totalValue": total_value,
                    "classification": "αχαρακτηριστο",
                    "AFM_issuer": vatissuer,
                    "Name_issuer": Name_issuer,
                    "paymentMethodType": payment_method_type
                }
                sig = _doc_signature(row)
                if sig not in seen_signatures:
                    seen_signatures.add(sig)
                    all_rows.append(row)
                    page_rows.append(row)
                if mark_val and (not page_max_mark or str(mark_val).isdigit() and int(mark_val) > int(page_max_mark or 0)):
                    page_max_mark = mark_val

        cursors = _extract_pagination_cursors(root)
        next_partition_key = cursors.get("nextPartitionKey") or ""
        next_row_key = cursors.get("nextRowKey") or ""
        next_partition_token = cursors.get("nextPartitionToken") or ""

        if next_partition_key or next_row_key:
            params_docs.pop("nextPartitionToken", None)
            if next_partition_key:
                params_docs["nextPartitionKey"] = next_partition_key
            else:
                params_docs.pop("nextPartitionKey", None)
            if next_row_key:
                params_docs["nextRowKey"] = next_row_key
            else:
                params_docs.pop("nextRowKey", None)
            if debug:
                print("[RequestDocs] nextPartitionKey:", params_docs.get("nextPartitionKey", ""))
                print("[RequestDocs] nextRowKey:", params_docs.get("nextRowKey", ""))
            continue

        if next_partition_token:
            params_docs.pop("nextPartitionKey", None)
            params_docs.pop("nextRowKey", None)
            params_docs["nextPartitionToken"] = next_partition_token
            if debug: print("[RequestDocs] NextPartitionToken:", params_docs["nextPartitionToken"])
            continue

        # AADE fallback: sometimes pagination cursors are omitted even though more
        # rows exist. Re-run the same date window using the highest MARK we saw as
        # the new lower-bound cursor.
        page_count = len(page_rows)
        can_resume_from_mark = (
            page_count > 0
            and page_max_mark
            and page_max_mark != current_mark
            and page_max_mark not in resume_guard_marks
        )
        if can_resume_from_mark:
            resume_guard_marks.add(page_max_mark)
            current_mark = page_max_mark
            params_docs = {"mark": current_mark, "dateFrom": date_from, "dateTo": date_to}
            if debug:
                print("[RequestDocs] Fallback resume from MARK:", current_mark, "page rows:", page_count)
            continue

        break

    return all_rows

def _fetch_transmitted_docs(mark: str, date_from: str, date_to: str, aade_user: str, aade_key: str) -> set:
    """Fetch transmitted marks in parallel."""
    URL_REQUEST_TRANSMITTED = "https://mydatapi.aade.gr/myDATA/RequestTransmittedDocs"
    headers = {"aade-user-id": aade_user, "Ocp-Apim-Subscription-Key": aade_key}
    
    date_to_docs = datetime.strptime(date_to, "%d/%m/%Y")
    date_to_trans = date_to_docs + relativedelta(months=3)
    DATE_TO_TRANS = date_to_trans.strftime("%d/%m/%Y")
    params_trans = {"mark": mark, "dateFrom": date_from, "dateTo": DATE_TO_TRANS}
    
    transmitted_marks = set()
    resp_trans = requests.get(URL_REQUEST_TRANSMITTED, params=params_trans, headers=headers)
    if resp_trans.status_code == 200 and resp_trans.content:
        root_trans = ET.fromstring(resp_trans.content)
        for elem in root_trans.iter():
            if elem.text:
                text = _safe_strip(elem.text)
                local = elem.tag.split("}", 1)[-1] if "}" in elem.tag else elem.tag
                if local.lower() == "invoicemark":
                    transmitted_marks.add(text)
                elif text.isdigit() and len(text) == 15:
                    transmitted_marks.add(text)
    return transmitted_marks


def _fetch_e3_info(mark: str, date_from: str, date_to: str, aade_user: str, aade_key: str, debug: bool = False) -> dict:
    """Fetch RequestE3Info (paginated) and derive per-mark classification using E3 columns.

    Rule: If E3 column 'κατηγορία' == 'ΜΗ' AND description column contains
    'ΧΑΡΑΚΤΗΡΙΣΜΕΝΑ ΕΞΟΔΑ' then treat mark as 'αχαρακτηριστο', otherwise 'χαρακτηρισμενο'.
    Returns mapping: mark -> classification ('αχαρακτηριστο'|'χαρακτηρισμενο').
    """
    URL_REQUEST_E3 = "https://mydatapi.aade.gr/myDATA/RequestE3Info"
    headers = {"aade-user-id": aade_user, "Ocp-Apim-Subscription-Key": aade_key}

    date_to_docs = datetime.strptime(date_to, "%d/%m/%Y")
    date_to_trans = date_to_docs + relativedelta(months=3)
    DATE_TO_TRANS = date_to_trans.strftime("%d/%m/%Y")
    params = {"mark": mark, "dateFrom": date_from, "dateTo": DATE_TO_TRANS}

    mark_class = {}

    ns = {'ns': 'http://www.aade.gr/myDATA/invoice/v1.0'}

    while True:
        resp = requests.get(URL_REQUEST_E3, params=params, headers=headers)
        if debug: print(f"[RequestE3Info] Status: {resp.status_code}")
        if resp.status_code != 200:
            # nothing to parse on error; return what we have
            if debug: print(f"[RequestE3Info] HTTP {resp.status_code}")
            return mark_class

        if resp.content:
            try:
                root = ET.fromstring(resp.content)
            except Exception:
                return mark_class

            # Prefer structured parsing: each <E3Info> contains V_Mark and V_Class_Category
            for e3 in root.findall('.//ns:E3Info', ns) or root.findall('.//E3Info'):
                try:
                    vm = e3.findtext('ns:V_Mark', default=None, namespaces=ns) or e3.findtext('V_Mark') or ""
                except Exception:
                    vm = e3.findtext('V_Mark') if e3.find('V_Mark') is not None else ""
                vm = _safe_strip(vm)

                try:
                    vcat = e3.findtext('ns:V_Class_Category', default=None, namespaces=ns) or e3.findtext('V_Class_Category') or ""
                except Exception:
                    vcat = e3.findtext('V_Class_Category') if e3.find('V_Class_Category') is not None else ""
                vcat = _safe_strip(vcat).upper()

                if not vm:
                    # fallback: search for a 15-digit mark anywhere inside this E3Info
                    text_blob = ''.join([_safe_strip(x.text) for x in e3.iter() if x.text])
                    m = re.search(r"(\d{15})", text_blob)
                    vm = m.group(1) if m else ""

                if not vm:
                    continue

                # default classification
                classification = "χαρακτηρισμενο"
                if vcat.startswith("ΜΗ") and "ΧΑΡΑΚΤΗΡΙΣΜΕΝΑ" in vcat:
                    classification = "αχαρακτηριστο"

                # If multiple E3Info rows for same mark, prefer any αχαρακτηριστο
                prev = mark_class.get(vm)
                if prev != "αχαρακτηριστο":
                    mark_class[vm] = classification

        # pagination: find nextPartitionToken by local name irrespective of namespace
        next_token = None
        for elem in root.iter():
            tag = elem.tag
            lname = tag.split('}', 1)[-1] if '}' in tag else tag
            if lname == 'nextPartitionToken' and elem.text:
                next_token = elem.text.strip()
                break

        if next_token:
            params["nextPartitionToken"] = next_token
            if debug: print("[RequestE3Info] NextPartitionToken:", params["nextPartitionToken"])
            continue
        break

    return mark_class

@monitor_resources('request_docs')
def request_docs(
    date_from: str,
    date_to: str,
    mark: str,
    aade_user: str,
    aade_key: str,
    debug: bool = False,
    save_excel: bool = True,
    out_filename: str = "invoices_vat_summary_classified.xlsx"
) -> Tuple[List[dict], List[dict]]:
    """
    Returns:
        all_rows_json, summary_json  # JSON-ready with comma decimals
    Also saves Excel with numeric columns for Καθαρή Αξία, ΦΠΑ, Σύνολο
    """
    # --- Step 1 & 2: Parallel fetch ---
    with ThreadPoolExecutor(max_workers=3) as executor:
        docs_future = executor.submit(_fetch_request_docs, mark, date_from, date_to, aade_user, aade_key, debug)
        trans_future = executor.submit(_fetch_transmitted_docs, mark, date_from, date_to, aade_user, aade_key)
        e3_future = executor.submit(_fetch_e3_info, mark, date_from, date_to, aade_user, aade_key, debug)

        all_rows = docs_future.result()
        transmitted_marks = trans_future.result()
        e3_map = e3_future.result()

    # --- Step 3: Classification update ---
    # Apply E3-based classification first (authoritative per user rule)
    for row in all_rows:
        m = row["mark"].strip()
        if m in e3_map:
            row["classification"] = e3_map.get(m, row["classification"])
        elif m in transmitted_marks:
            row["classification"] = "χαρακτηρισμενο"

    # --- Step 4: Summary aggregation ---
    summary_rows = {}
    for row in all_rows:
        mark_val = row["mark"]
        if mark_val not in summary_rows:
            summary_rows[mark_val] = dict(row)
        else:
            sr = summary_rows[mark_val]
            sr["totalNetValue"] = round(sr.get("totalNetValue", 0) + row.get("totalNetValue", 0), 2)
            sr["totalVatAmount"] = round(sr.get("totalVatAmount", 0) + row.get("totalVatAmount", 0), 2)
            sr["totalValue"] = round(sr.get("totalValue", 0) + row.get("totalValue", 0), 2)
            if row.get("classification") == "χαρακτηρισμενο":
                sr["classification"] = "χαρακτηρισμενο"

    summary_list = list(summary_rows.values())

    # --- Step 5: Save Excel with numeric columns and Greek headers ---
    if save_excel:
        all_rows_excel = [{
            "MARK": r["mark"], "Ημερομηνία": r["issueDate"], "Σειρά": r["series"], "ΑΑ": r["aa"],
            "Τύπος": r["type"], "Καθαρή Αξία": r["totalNetValue"], "ΦΠΑ": r["totalVatAmount"],
            "Σύνολο": r["totalValue"], "Κατάσταση": r["classification"],
            "AFM Εκδότη": r["AFM_issuer"], "Όνομα Εκδότη": r["Name_issuer"]
        } for r in all_rows]

        summary_excel = [{
            "MARK": s["mark"], "Ημερομηνία": s["issueDate"], "Σειρά": s["series"], "ΑΑ": s["aa"],
            "Τύπος": s["type"], "Καθαρή Αξία": s["totalNetValue"], "ΦΠΑ": s["totalVatAmount"],
            "Σύνολο": s["totalValue"], "Κατάσταση": s["classification"],
            "AFM Εκδότη": s["AFM_issuer"], "Όνομα Εκδότη": s["Name_issuer"]
        } for s in summary_list]

        with pd.ExcelWriter(out_filename, engine="openpyxl") as writer:
            pd.DataFrame(all_rows_excel).to_excel(writer, sheet_name="detailed", index=False)
            pd.DataFrame(summary_excel).to_excel(writer, sheet_name="summary", index=False)
        if debug:
            print(f"Saved {len(all_rows)} detailed rows and {len(summary_list)} summary rows to '{out_filename}'.")

    # --- Step 6: JSON-ready with comma decimals ---
    all_rows_json = []
    for r in all_rows:
        rr = dict(r)
        rr["totalNetValue"] = format_decimal_comma(rr["totalNetValue"])
        rr["totalVatAmount"] = format_decimal_comma(rr["totalVatAmount"])
        rr["totalValue"] = format_decimal_comma(rr["totalValue"])
        all_rows_json.append(rr)

    summary_json = []
    for s in summary_list:
        ss = dict(s)
        ss["totalNetValue"] = format_decimal_comma(ss["totalNetValue"])
        ss["totalVatAmount"] = format_decimal_comma(ss["totalVatAmount"])
        ss["totalValue"] = format_decimal_comma(ss["totalValue"])
        summary_json.append(ss)

    return all_rows_json, summary_json
