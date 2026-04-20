# app.py (ολοκληρωμένο, με ενσωματωμένη λογική για per-line categorization -> epsilon per-vat files)
import os
import sys
import logging

# Load environment variables from .env file first
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Load runtime secrets from Infisical using only Infisical bootstrap vars
try:
    from infisical_bootstrap import bootstrap_infisical_secrets
    bootstrap_infisical_secrets()
except Exception as _infisical_exc:
    logging.getLogger(__name__).warning("Infisical bootstrap unavailable: %s", _infisical_exc)

# Warn if MASTER_ENCRYPTION_KEY is missing or empty
if not os.getenv("MASTER_ENCRYPTION_KEY") or os.getenv("MASTER_ENCRYPTION_KEY") == "":
     logging.warning("MASTER_ENCRYPTION_KEY is missing or empty! Decryption will fail.")

import json
import traceback
import base64
import re
import time
import errno
from urllib.parse import urlsplit, urlparse, urlunparse
from logging.handlers import RotatingFileHandler
import datetime
from typing import Any, List, Dict, Optional, Tuple
from datetime import datetime as _dt
from werkzeug.utils import secure_filename
from datetime import timezone
from markupsafe import escape, Markup
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    send_file,
    flash,
    jsonify,
    session,
    after_this_request,
    Response,
    stream_with_context,
)
import tempfile
import zipfile
import shutil
from scraper import scrape_wedoconnect, scrape_mydatapi, scrape_einvoice, scrape_impact, scrape_epsilon, scrape_pegcloud, scrape_einvoicing_gr, scrape_vsgr, scrape_megasoft
import requests
import pandas as pd
from shutil import move
import importlib
import io
from activity_monitor import monitor_resources, start_request_monitoring, end_request_monitoring
import csv
import unicodedata
import secrets
import qrcode
from epsilon_bridge_multiclient_strict import (
    run_and_report_dynamic,
    export_multiclient_strict,
    build_preview_strict_multiclient,
    _load_client_map,           # για ανάγνωση client_db (υπάρχει ήδη στο αρχείο σου)
    _safe_json_read,            # για ανάγνωση json εφόσον χρειαστεί
    _norm_afm,   # <-- απαιτείται
    # προαιρετικά: export_multiclient_strict
)
from scraper_receipt import detect_and_scrape as scrape_receipt
# local mydata helper
from fetch import request_docs
import sys, subprocess, json
from pathlib import Path
# --- Lock + current_app imports (paste here) ---
import threading
# thread-local storage used by background workers to remember which
# group directory they should treat as "active".  Without this the
# fetch thread loses the request/session context and get_group_base_dir()
# falls back to the global DATA_DIR, which means invoices end up in the
# wrong place (the complaint "έχει χάσει το scope που να τα γράφει").
# The helper functions below let the thread set/read an override.
_thread_locals = threading.local()

def _set_thread_group_base_dir(path: str):
    try:
        _thread_locals.group_base_dir = path
    except Exception:
        pass

def _get_thread_group_base_dir():
    return getattr(_thread_locals, 'group_base_dir', None)

try:
    from filelock import FileLock  # προτιμώμενο, cross-process
except Exception:
    # fallback: lightweight in-process lock usable as context manager
    class FileLock:
        def __init__(self, path, timeout=None):
            self.path = path
            self.timeout = timeout
            self._lock = threading.Lock()

        def acquire(self, timeout=None):
            if timeout is None:
                return self._lock.acquire()
            try:
                return self._lock.acquire(timeout=float(timeout))
            except TypeError:
                return self._lock.acquire()

        def release(self):
            try:
                self._lock.release()
            except RuntimeError:
                pass

        def __enter__(self):
            self.acquire(timeout=self.timeout)
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.release()

from flask import current_app
from epsilon_bridge_multiclient_strict import build_preview_rows_for_ui
import utils as utils
from utils import decode_qr_from_file, decode_qr_payloads, extract_mark

# Firebase & Admin imports
import firebase_config
import admin_panel
from encryption import encrypt_data, decrypt_data, encrypt_data_with_group_key, decrypt_data_with_group_key

# Import login_required early for decorator usage
try:
    from flask_login import login_required
except ImportError:
    # Fallback: create a no-op decorator if flask_login not available
    def login_required(f):
        return f

# --- end lock + current_app imports ---
# global helpers -------------------------------------------------------------

def float_from_comma(value):
    """Convert numbers that may use comma as decimal separator and dots as
    thousands separators into a float.  Examples:
        "5.645,00" -> 5645.0
        "56,45"    -> 56.45
        "1.234.567" -> 1234567.0
    Returns 0.0 on failure or when value is ``None``.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


INVOICE_TYPE_LABELS = {
    "1.1": "Τιμολόγιο Πώλησης",
    "1.2": "Τιμολόγιο Πώλησης / Ενδοκοινοτικές Παραδόσεις",
    "1.3": "Τιμολόγιο Πώλησης / Παραδόσεις Τρίτων Χωρών",
    "1.4": "Τιμολόγιο Πώλησης / Πώληση για Λογαριασμό Τρίτων",
    "1.5": "Τιμολόγιο Πώλησης / Εκκαθάριση Πωλήσεων Τρίτων",
    "1.6": "Τιμολόγιο Πώλησης / Συμπληρωματικό Παραστατικό",
    "2.1": "Τιμολόγιο Παροχής Υπηρεσιών",
    "2.2": "Τιμολόγιο Παροχής / Ενδοκοινοτική Παροχή Υπηρεσιών",
    "2.3": "Τιμολόγιο Παροχής / Παροχή Υπηρεσιών σε λήπτη Τρίτης Χώρας",
    "2.4": "Τιμολόγιο Παροχής / Συμπληρωματικό Παραστατικό",
    "3.1": "Τίτλος Κτήσης (μη υπόχρεος Εκδότης)",
    "3.2": "Τίτλος Κτήσης (άρνηση έκδοσης από υπόχρεο Εκδότη)",
    "5.1": "Πιστωτικό Τιμολόγιο / Συσχετιζόμενο",
    "5.2": "Πιστωτικό Τιμολόγιο / Μη Συσχετιζόμενο",
    "6.1": "Στοιχείο Αυτοπαράδοσης",
    "6.2": "Στοιχείο Ιδιοχρησιμοποίησης",
    "7.1": "Συμβόλαιο - Έσοδο",
    "8.1": "Ενοίκια - Έσοδο",
    "8.2": "Τέλος ανθεκτικότητας κλιματικής κρίσης",
    "8.4": "Απόδειξη Είσπραξης POS",
    "8.5": "Απόδειξη Επιστροφής POS",
    "8.6": "Δελτίο Παραγγελίας Εστίασης",
    "9.3": "Δελτίο Αποστολής",
    "11.1": "ΑΛΠ",
    "11.2": "ΑΠΥ",
    "11.3": "Απλοποιημένο Τιμολόγιο",
    "11.4": "Πιστωτικό Στοιχείο Λιανικής",
    "11.5": "Απόδειξη Λιανικής Πώλησης για Λογαριασμό Τρίτων",
    "13.1": "Έξοδα - Αγορές Λιανικών Συναλλαγών ημεδαπής / αλλοδαπής",
    "13.2": "Παροχή Λιανικών Συναλλαγών ημεδαπής / αλλοδαπής",
    "13.3": "Κοινόχρηστα",
    "13.4": "Συνδρομές",
    "13.30": "Παραστατικά Οντότητας ως Αναγράφονται από την ίδια",
    "13.31": "Πιστωτικό Στοιχείο Λιανικής ημεδαπής / αλλοδαπής",
    "14.1": "Τιμολόγιο / Ενδοκοινοτικές Αποκτήσεις",
    "14.2": "Τιμολόγιο / Αποκτήσεις Τρίτων Χωρών",
    "14.3": "Τιμολόγιο / Ενδοκοινοτική Λήψη Υπηρεσιών",
    "14.4": "Τιμολόγιο / Λήψη Υπηρεσιών Τρίτων Χωρών",
    "14.5": "ΕΦΚΑ και λοιποί Ασφαλιστικοί Οργανισμοί",
    "14.30": "Παραστατικά Οντότητας ως Αναγράφονται από την ίδια",
    "14.31": "Πιστωτικό ημεδαπής / αλλοδαπής",
    "15.1": "Συμβόλαιο - Έξοδο",
    "16.1": "Ενοίκιο Έξοδο",
    "17.1": "Μισθοδοσία",
    "17.2": "Αποσβέσεις",
    "17.3": "Λοιπές Εγγραφές Τακτοποίησης Εσόδων - Λογιστική Βάση",
    "17.4": "Λοιπές Εγγραφές Τακτοποίησης Εσόδων - Φορολογική Βάση",
    "17.5": "Λοιπές Εγγραφές Τακτοποίησης Εξόδων - Λογιστική Βάση",
    "17.6": "Λοιπές Εγγραφές Τακτοποίησης Εξόδων - Φορολογική Βάση",
}


def map_invoice_type_label(value: Any) -> str:
    code = str(value or "").strip()
    if not code:
        return ""
    return INVOICE_TYPE_LABELS.get(code, code)

# --- Compatibility shim: unify invoice-scraper vs receipt-scraper usage ---
# Αυτό το snippet προσπαθεί να χρησιμοποιήσει:
# 1) scrape_receipt από module scraper (αν υπάρχει) ή
# 2) detect_and_scrape από scraper_receipt.py (αν υπάρχει)
# και παρέχει την helper συνάρτηση call_scrape_receipt(mark)
import importlib
scrape_receipt_callable = None

def _client_map_for_vat(vat: str):
    """
    Γυρνά dict σαν του builder:
      {"by_afm": {...}, "by_id": set([...]), "cols": [...]}
    """
    try:
        paths = resolve_paths_for_vat(str(vat), base_invoices_dir=group_path("epsilon"))
        return bridge_load_client_map(paths["client_db"])
    except Exception as e:
        current_app.logger.warning("client_db load failed for VAT %s: %s", vat, e)
        return {"by_afm": {}, "by_id": set(), "cols": []}

def strip_server_totals(html: str) -> str:
    """Αφαιρεί οτιδήποτε <tfoot> και όποια τυχόν 'ΣΥΝΟΛΑ' γραμμή έχει
    τρυπώσει στο <tbody>, ώστε να αφήσουμε ΜΟΝΟ τα δυναμικά totals client-side."""
    try:
        if not html:
            return html
        # Καθάρισε προϋπάρχον <tfoot> (server-side)
        html = re.sub(r"(?is)<tfoot[\s\S]*?</tfoot>", "", html, flags=re.I)

        # Καθάρισε όποια γραμμή tbody περιέχει τη λέξη ΣΥΝΟΛΑ (αν έχει μπει ως tr)
        html = re.sub(r"(?is)<tr[^>]*>[^<]*συνολ[άα][^<]*</tr>", "", html, flags=re.I)

        return html
    except Exception:
        return html
def _normalize_receipt_res(res):
    """Normalize the various possible outputs of the receipt scraper into a dict."""
    try:
        if not res or not isinstance(res, dict):
            return {"ok": False, "error": "scraper returned no dict"}
        return {
            "ok": True,
            "MARK": res.get("MARK") or res.get("mark") or res.get("invoice_id") or res.get("id"),
            "issue_date": res.get("issue_date") or res.get("issueDate") or res.get("date") or "",
            "issuer_vat": res.get("issuer_vat") or res.get("issuer_vat_number") or res.get("issuerAFM") or res.get("AFM") or "",
            "total_amount": res.get("total_amount") or res.get("totalAmount") or res.get("totalValue") or res.get("amount") or "",
            "doc_type": res.get("doc_type") or res.get("docType") or res.get("doc_type_readable") or "",
            "is_invoice": bool(res.get("is_invoice")) if "is_invoice" in res else False,
            "raw": res
        }
    except Exception as e:
        return {"ok": False, "error": f"normalize failed: {e}"}

# try import scrape_receipt from scraper
try:
    _scraper_mod = importlib.import_module("scraper")
    if hasattr(_scraper_mod, "scrape_receipt") and callable(getattr(_scraper_mod, "scrape_receipt")):
        def call_scrape_receipt(mark):
            try:
                res = _scraper_mod.scrape_receipt(mark)
                return _normalize_receipt_res(res)
            except Exception as e:
                return {"ok": False, "error": f"scraper.scrape_receipt failed: {e}"}
        scrape_receipt_callable = call_scrape_receipt
except Exception:
    pass

# fallback to scraper_receipt.detect_and_scrape
if scrape_receipt_callable is None:
    try:
        _sr = importlib.import_module("scraper_receipt")
        if hasattr(_sr, "detect_and_scrape") and callable(getattr(_sr, "detect_and_scrape")):
            def call_scrape_receipt(mark):
                try:
                    res = _sr.detect_and_scrape(mark)
                    return _normalize_receipt_res(res)
                except Exception as e:
                    return {"ok": False, "error": f"scraper_receipt.detect_and_scrape failed: {e}"}
            scrape_receipt_callable = call_scrape_receipt
    except Exception:
        scrape_receipt_callable = None

# Now call_scrape_receipt(mark) is available if either module provided the functionality.


# Remote QR session management (desktop ↔ mobile bridge)
REMOTE_QR_SESSIONS: Dict[str, Dict[str, Any]] = {}
REMOTE_QR_LOCK = threading.Lock()
REMOTE_QR_MAX_SESSIONS = 200
REMOTE_QR_SESSION_TTL = datetime.timedelta(minutes=15)
REMOTE_QR_IDLE_TTL = datetime.timedelta(minutes=5)
REMOTE_QR_REMOTE_STALE = datetime.timedelta(seconds=45)


def _token_hint(value: Optional[str]) -> str:
    if not value:
        return "-"
    try:
        text = str(value)
    except Exception:
        return "?"
    return text[:8] + "…" if len(text) > 8 else text


def _preferred_request_scheme() -> str:
    """Resolve the most appropriate scheme for external links."""
    forwarded_proto = request.headers.get("X-Forwarded-Proto") if request else None
    if forwarded_proto:
        return forwarded_proto.split(",")[0].strip() or request.scheme
    render_external_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_BASE_URL")
    if render_external_url:
        try:
            return urlsplit(render_external_url).scheme or request.scheme
        except Exception:
            return request.scheme
    return request.scheme


def _build_external_url(endpoint: str, **values: Any) -> str:
    """Build an external URL that respects proxy headers or explicit overrides."""
    base_override = os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    relative = url_for(endpoint, _external=False, **values)
    if base_override:
        base = base_override.rstrip("/")
        return f"{base}{relative}"

    scheme = _preferred_request_scheme()
    forwarded_host = request.headers.get("X-Forwarded-Host") if request else None
    host = (forwarded_host.split(",")[0].strip() if forwarded_host else None) or request.host
    return f"{scheme}://{host}{relative}"


def _generate_qr_data_uri(text: str) -> str:
    if not text:
        return ""
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_Q,
            box_size=6,
            border=2,
        )
        qr.add_data(text)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception as exc:
        current_app.logger.warning("Failed to render QR data URI: %s", exc)
        return ""


def _normalize_remote_mode(mode: Any) -> str:
    try:
        value = str(mode or "").strip().lower()
    except Exception:
        value = ""
    return "receipts" if value == "receipts" else "invoices"


def _remote_qr_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _parse_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return None


# --- Remote processing lock helpers (desktop saves signal mobile to pause) ---
def _signal_remote_processing_start(timeout_seconds: int = 30) -> None:
    owner = session.get("_remote_qr_owner")
    if not owner:
        return
    deadline = _remote_qr_now() + datetime.timedelta(seconds=timeout_seconds)
    with REMOTE_QR_LOCK:
        for entry in REMOTE_QR_SESSIONS.values():
            if entry.get("owner_token") == owner:
                entry["excel_updating"] = True
                entry["excel_updating_until"] = deadline


def _signal_remote_processing_end() -> None:
    owner = session.get("_remote_qr_owner")
    if not owner:
        return
    with REMOTE_QR_LOCK:
        for entry in REMOTE_QR_SESSIONS.values():
            if entry.get("owner_token") == owner:
                entry["excel_updating"] = False
                entry["excel_updating_until"] = None


def _is_remote_processing(entry: Dict[str, Any]) -> bool:
    now = _remote_qr_now()
    processing_until = entry.get("excel_updating_until")
    if processing_until and isinstance(processing_until, datetime.datetime):
        if now < processing_until:
            return True
    # auto-clear stale flag
    entry["excel_updating"] = False
    entry["excel_updating_until"] = None
    return False


def _normalize_scanned_value(raw: str) -> str:
    try:
        text = (raw or "").strip()
    except Exception:
        return ""
    if not text:
        return ""

    if not text.lower().startswith(("http://", "https://")):
        return text

    try:
        parsed = urlparse(text)
    except Exception:
        return text

    hostname = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if "epsilondigital" in hostname:
        path = re.sub(r":\d+(?=$|/)", "", path)
        path = re.sub(r"/{2,}", "/", path or "/")
        if not path.startswith("/"):
            path = "/" + path
        trimmed = path.rstrip("/")
        if not trimmed:
            trimmed = "/"
        parsed = parsed._replace(path=trimmed, query="", fragment="")
        return urlunparse(parsed)

    return urlunparse(parsed)


def _ensure_remote_owner_token() -> str:
    token = session.get("_remote_qr_owner")
    if not token:
        token = secrets.token_urlsafe(18)
        session["_remote_qr_owner"] = token
    return token


def _purge_remote_sessions() -> None:
    now = _remote_qr_now()
    with REMOTE_QR_LOCK:
        expired: List[str] = []
        for sid, data in list(REMOTE_QR_SESSIONS.items()):
            expires_at: Optional[datetime.datetime] = data.get("expires_at")
            last_seen: datetime.datetime = (
                data.get("last_seen")
                or data.get("created_at")
                or now
            )
            if expires_at and now > expires_at:
                expired.append(sid)
                continue
            if now - last_seen > REMOTE_QR_IDLE_TTL:
                expired.append(sid)

        for sid in expired:
            REMOTE_QR_SESSIONS.pop(sid, None)

        excess = len(REMOTE_QR_SESSIONS) - REMOTE_QR_MAX_SESSIONS
        if excess > 0:
            # Sort by last_seen (fallback to created_at) ascending so the stalest are evicted first.
            sorted_items = sorted(
                REMOTE_QR_SESSIONS.items(),
                key=lambda item: (
                    item[1].get("last_seen")
                    or item[1].get("created_at")
                    or now
                ),
            )
            for sid, _ in sorted_items[:excess]:
                REMOTE_QR_SESSIONS.pop(sid, None)


def _sanitize_summary_state(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        return {}

    def _clip_text(text: Any, limit: int = 240) -> str:
        try:
            rendered = str(text or "")
        except Exception:
            rendered = ""
        if len(rendered) > limit:
            return rendered[: limit - 1] + "…"
        return rendered

    state: Dict[str, Any] = {
        "visible": bool(value.get("visible")),
        "mark": _clip_text(value.get("mark"), 120),
        "can_save": bool(value.get("can_save")),
        "can_close": bool(value.get("can_close")),
    }

    totals = value.get("totals")
    if isinstance(totals, dict):
        state["totals"] = {
            "net": _clip_text(totals.get("net"), 80),
            "vat": _clip_text(totals.get("vat"), 80),
            "total": _clip_text(totals.get("total"), 80),
        }

    details = value.get("details")
    if isinstance(details, dict):
        cleaned_details: Dict[str, str] = {}
        for key, raw in list(details.items())[:20]:
            try:
                key_text = str(key)
            except Exception:
                continue
            cleaned_details[key_text] = _clip_text(raw, 200)
        if cleaned_details:
            state["details"] = cleaned_details

    banner = value.get("reclassification_banner") or {}
    if isinstance(banner, dict):
        state["reclassification_banner"] = {
            "visible": bool(banner.get("visible")),
            "text": _clip_text(banner.get("text"), 280),
        }

    lines: List[Dict[str, Any]] = []
    for entry in value.get("lines", []):
        if not isinstance(entry, dict):
            continue
        line_id_raw = entry.get("id") or entry.get("line_id") or entry.get("lid")
        try:
            line_id = str(line_id_raw or "").strip()
        except Exception:
            line_id = ""
        if not line_id:
            continue
        line: Dict[str, Any] = {"id": line_id[: 80]}
        if entry.get("index") is not None:
            line["index"] = int(entry.get("index")) if isinstance(entry.get("index"), int) else entry.get("index")
        if entry.get("description") is not None:
            line["description"] = _clip_text(entry.get("description"), 360)
        if entry.get("amount") is not None:
            line["amount"] = _clip_text(entry.get("amount"), 120)
        if entry.get("vat") is not None:
            line["vat"] = _clip_text(entry.get("vat"), 120)
        if entry.get("category") is not None:
            line["category"] = _clip_text(entry.get("category"), 160)
        if entry.get("selected_label") is not None:
            line["selected_label"] = _clip_text(entry.get("selected_label"), 160)

        options: List[Dict[str, str]] = []
        for opt in entry.get("options", []):
            if not isinstance(opt, dict):
                continue
            val = _clip_text(opt.get("value"), 160)
            label = _clip_text(opt.get("label"), 220)
            options.append({"value": val, "label": label})
            if len(options) >= 40:
                break
        if options:
            line["options"] = options

        lines.append(line)
        if len(lines) >= 80:
            break

    state["lines"] = lines
    state["updated_at"] = _clip_text(value.get("updated_at") or value.get("timestamp") or "", 160)

    warnings: List[Dict[str, Any]] = []
    for entry in value.get("warnings", []):
        if not isinstance(entry, dict):
            continue
        warning: Dict[str, Any] = {
            "visible": bool(entry.get("visible", True)),
        }
        modal_id = entry.get("id") or entry.get("modal_id") or entry.get("modalId")
        if modal_id is not None:
            warning["id"] = _clip_text(modal_id, 160)
        title = entry.get("title") or entry.get("heading")
        if title is not None:
            warning["title"] = _clip_text(title, 200)
        body = entry.get("body") or entry.get("message")
        if body is not None:
            warning["body"] = _clip_text(body, 600)
        severity = entry.get("severity")
        if severity is not None:
            warning["severity"] = _clip_text(severity, 40)

        actions: List[Dict[str, Any]] = []
        for action in entry.get("actions", []):
            if not isinstance(action, dict):
                continue
            label = action.get("label") or action.get("text")
            if label is None:
                continue
            button: Dict[str, Any] = {
                "label": _clip_text(label, 160)
            }
            action_id = action.get("id") or action.get("button_id") or action.get("buttonId")
            if action_id is not None:
                button["id"] = _clip_text(action_id, 160)
            action_key = action.get("action") or action.get("action_id") or action.get("actionId")
            if action_key is not None:
                button["action"] = _clip_text(action_key, 160)
            role = action.get("role")
            if role is not None:
                button["role"] = _clip_text(role, 80)
            actions.append(button)
            if len(actions) >= 6:
                break
        if actions:
            warning["actions"] = actions

        warnings.append(warning)
        if len(warnings) >= 6:
            break

    if warnings:
        state["warnings"] = warnings

    return state


def _sanitize_remote_control(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    control_type = value.get("type")
    try:
        normalized_type = str(control_type or "").strip().lower()
    except Exception:
        normalized_type = ""
    allowed = {
        "summary_set_category",
        "summary_save",
        "summary_close",
        "summary_confirm",
        "auto_submit_set",
        "auto_submit_toggle",
        "warning_action",
    }
    if normalized_type not in allowed:
        return None

    def _clip(value: Any, limit: int) -> str:
        try:
            text = str(value or "").strip()
        except Exception:
            text = ""
        if len(text) > limit:
            return text[:limit]
        return text

    control: Dict[str, Any] = {"type": normalized_type}
    if normalized_type == "summary_set_category":
        try:
            line_id = str(value.get("line_id") or value.get("id") or "").strip()
        except Exception:
            line_id = ""
        try:
            category = str(value.get("category") or value.get("value") or "").strip()
        except Exception:
            category = ""
        if not line_id:
            return None
        control["line_id"] = line_id[: 80]
        control["category"] = category[: 200]
    elif normalized_type == "auto_submit_set":
        candidates = (
            value.get("enabled"),
            value.get("value"),
            value.get("state"),
            value.get("target"),
        )
        flag: Optional[bool] = None
        for candidate in candidates:
            if flag is not None:
                break
            parsed = _parse_bool(candidate)
            if parsed is not None:
                flag = parsed
        if flag is None:
            try:
                text = str(next((c for c in candidates if c is not None), "")).strip().lower()
            except Exception:
                text = ""
            if text in {"on", "yes"}:
                flag = True
            elif text in {"off", "no"}:
                flag = False
        if flag is None:
            return None
        control["enabled"] = bool(flag)
    elif normalized_type == "warning_action":
        control["modal_id"] = _clip(value.get("modal_id") or value.get("modalId") or value.get("id"), 160)
        control["button_id"] = _clip(value.get("button_id") or value.get("buttonId"), 160)
        control["action"] = _clip(value.get("action") or value.get("action_id") or value.get("actionId"), 160)
        control["role"] = _clip(value.get("role"), 80)
        control["label"] = _clip(value.get("label") or value.get("text"), 160)
    return control


def _prime_remote_summary_state(
    mark_value: str,
    modal_summary: Optional[Dict[str, Any]],
    invoice_lines: Optional[List[Dict[str, Any]]],
    categories: Optional[List[Any]],
    category_labels: Optional[Dict[str, Any]],
    warning_text: Optional[str],
    allow_edit_existing: bool,
    force_edit_active: bool,
):
    owner = session.get("_remote_qr_owner")
    if not owner:
        return

    with REMOTE_QR_LOCK:
        targets = [
            entry
            for entry in REMOTE_QR_SESSIONS.values()
            if entry.get("owner_token") == owner
        ]
    if not targets:
        return

    def _text(value: Any) -> str:
        try:
            return str(value or "").strip()
        except Exception:
            return ""

    def _label_for_category(value: Any) -> str:
        key = _text(value)
        if not key:
            return ""
        labels = category_labels or {}
        if isinstance(labels, dict) and key in labels:
            return _text(labels.get(key)) or key
        if key.startswith("custom_"):
            return key.replace("custom_", "").replace("_", " ")
        return key.replace("_", " ")

    mark_text = ""
    if isinstance(modal_summary, dict):
        for candidate in ("mark", "MARK", "Mark"):
            if modal_summary.get(candidate):
                mark_text = _text(modal_summary.get(candidate))
                if mark_text:
                    break
    if not mark_text:
        mark_text = _text(mark_value)

    options: List[Dict[str, str]] = []
    for cat in categories or []:
        value = _text(cat)
        if not value:
            continue
        options.append({"value": value, "label": _label_for_category(value)})

    lines_source: List[Dict[str, Any]] = []
    if isinstance(modal_summary, dict) and isinstance(modal_summary.get("lines"), list):
        lines_source = modal_summary.get("lines", [])
    elif isinstance(invoice_lines, list):
        lines_source = invoice_lines

    details: Dict[str, str] = {}
    if isinstance(modal_summary, dict):
        mapping = {
            "Α/Α": modal_summary.get("AA") or modal_summary.get("aa") or modal_summary.get("number"),
            "ΑΦΜ": modal_summary.get("AFM") or modal_summary.get("AFM_issuer"),
            "Επωνυμία": modal_summary.get("Name") or modal_summary.get("Name_issuer"),
            "Ημερομηνία": modal_summary.get("issueDate") or modal_summary.get("issue_date"),
            "Τύπος": modal_summary.get("type_name") or modal_summary.get("type"),
            "Σειρά": modal_summary.get("series"),
        }
        for key, value in mapping.items():
            text = _text(value)
            if text:
                details[key] = text

    totals = {}
    if isinstance(modal_summary, dict):
        totals = {
            "net": _text(modal_summary.get("totalNetValue")),
            "vat": _text(modal_summary.get("totalVatAmount")),
            "total": _text(modal_summary.get("totalValue")),
        }

    line_payload: List[Dict[str, Any]] = []
    for idx, line in enumerate(lines_source):
        if not isinstance(line, dict):
            continue
        line_id = _text(line.get("id") or line.get("line_id") or f"l{idx}")
        if not line_id:
            continue
        category_value = _text(line.get("category") or line.get("cat"))
        entry = {
            "id": line_id,
            "index": idx,
            "description": _text(line.get("description") or line.get("desc")),
            "amount": _text(line.get("amount") or line.get("total") or line.get("lineTotal")),
            "vat": _text(line.get("vat") or line.get("vatRate")),
            "category": category_value,
        }
        if category_value:
            entry["selected_label"] = _label_for_category(category_value)
        if options:
            entry["options"] = options
        line_payload.append(entry)

    payload: Dict[str, Any] = {
        "visible": bool(modal_summary),
        "mark": mark_text,
        "lines": line_payload,
        "can_save": bool(modal_summary),
        "can_close": True,
    }

    if totals:
        payload["totals"] = totals
    if details:
        payload["details"] = details

    if allow_edit_existing and not force_edit_active and mark_text:
        payload["reclassification_banner"] = {
            "visible": True,
            "text": f"Το MARK {mark_text} υπάρχει ήδη στο Excel. Θέλεις να τροποποιήσεις τον χαρακτηρισμό;",
        }

    warnings: List[Dict[str, Any]] = []
    warning_body = _text(warning_text)
    if warning_body:
        warnings.append(
            {
                "id": "afmWarningModal",
                "title": "Προειδοποίηση",
                "body": warning_body,
                "visible": True,
                "actions": [
                    {
                        "id": "afmModalConfirm",
                        "label": "Κατάλαβα",
                        "role": "ack",
                        "action": "warning_ack",
                    }
                ],
            }
        )
    if warnings:
        payload["warnings"] = warnings

    sanitized = _sanitize_summary_state(payload)
    if sanitized is None:
        sanitized = {}

    now = _remote_qr_now()
    with REMOTE_QR_LOCK:
        for entry in REMOTE_QR_SESSIONS.values():
            if entry.get("owner_token") != owner:
                continue
            entry["summary_state"] = sanitized
            entry["summary_version"] = (entry.get("summary_version") or 0) + 1
            entry["summary_last_delivered"] = 0
            entry["last_seen"] = now
            entry["expires_at"] = now + REMOTE_QR_SESSION_TTL

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

DATA_DIR = os.path.join(BASE_DIR, "data")
ADMIN_SYSTEM_DIR = os.path.join(DATA_DIR, 'system')
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(ADMIN_SYSTEM_DIR, exist_ok=True)
os.makedirs(UPLOADS_DIR, exist_ok=True)

CACHE_FILE = os.path.join(DATA_DIR, "invoices_cache.json")
SUMMARY_FILE = os.path.join(DATA_DIR, "summary.json")
CREDENTIALS_FILE = os.path.join(DATA_DIR, "credentials.json")
DEFAULT_EXCEL_FILE = os.path.join(UPLOADS_DIR, "invoices.xlsx")
ERROR_LOG = os.path.join(DATA_DIR, "error.log")
ACTIVITY_LOG = os.path.join(DATA_DIR, "activity.log")
EPSILON_JSON_PATH = os.path.join(DATA_DIR, 'epsilon_invoices.json')
EPSILON_EXCEL_PATH = os.path.join(DATA_DIR, 'epsilon_invoices.xlsx')
MARK_COUNTER_PATH = os.path.join(DATA_DIR, 'mark_counter.json')

AADE_USER_ENV = os.getenv("AADE_USER_ID", "")
AADE_KEY_ENV = os.getenv("AADE_SUBSCRIPTION_KEY", "")
MYDATA_ENV = (os.getenv("MYDATA_ENV") or "sandbox").lower()
ALLOWED_CLIENT_EXT = {'.xlsx', '.xls', '.csv'}



app = Flask(__name__, template_folder=TEMPLATES_DIR)
app.secret_key = os.getenv("FLASK_SECRET", "douradonis1997")
app.config["UPLOAD_FOLDER"] = UPLOADS_DIR
# Keep backend inactivity timeout aligned with frontend timeout.
app.config.setdefault('SESSION_TIMEOUT_SECONDS', int(os.getenv('SESSION_TIMEOUT_SECONDS', '900')))

# --- Initialize Logger ---
logger = logging.getLogger(__name__)
# configure both module-specific logger and the root logger so that
# unrelated modules (admin_panel, firebase_config, etc.) propagate to the
# same file.  previously only the logger for this module was wired,
# which is why admin_panel messages were never written.
logger.setLevel(logging.INFO)

# Create a rotating file handler
handler = RotatingFileHandler('firebed.log', maxBytes=10485760, backupCount=10)
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

# attach to this module's logger
logger.addHandler(handler)

# also attach to root logger so all loggers propagate by default
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
# avoid adding duplicate handlers if this code executed more than once
if handler not in root_logger.handlers:
    root_logger.addHandler(handler)
# --- end logger init ---

# --- Initialize DB and authentication ---
try:
    # local imports to avoid circulars during module import
    from models import db
    from auth import login_manager, auth_bp

    app.config.setdefault('SQLALCHEMY_DATABASE_URI', os.getenv('DATABASE_URL') or 'sqlite:///' + os.path.join(BASE_DIR, 'firebed.db'))
    app.config.setdefault('SQLALCHEMY_TRACK_MODIFICATIONS', False)

    db.init_app(app)
    login_manager.init_app(app)
    app.register_blueprint(auth_bp)

    # create tables if missing (safe during startup)
    with app.app_context():
        try:
            db.create_all()
        except Exception:
            # ignore DB creation errors during import; app can still run
            pass
    # Register a SQLAlchemy after_commit hook to record DB activity per-user.
    try:
        from sqlalchemy import event
        from sqlalchemy.orm import Session
        from flask_login import current_user
        from flask import session as flask_session
        import firebase_config as _fc
        from models import Group

        @event.listens_for(Session, 'after_commit')
        def _after_commit(session):
            # This is called after any commit; record that the current user
            # wrote to the DB and schedule an idle sync for their active group.
            try:
                if not getattr(current_user, 'is_authenticated', False):
                    return
                # active_group is group.name — translate to data_folder
                active_group_name = flask_session.get('active_group')
                if not active_group_name:
                    return
                grp = Group.query.filter_by(name=active_group_name).first()
                if not grp:
                    return
                _fc.firebase_record_db_activity(current_user.id, grp.data_folder)
            except Exception as e:
                try:
                    current_app.logger.debug('After commit: %s', e)
                except Exception:
                    pass
                return
    except Exception:
        # Not fatal; if SQLAlchemy hooks fail allow app to continue
        pass
except Exception:
    # if SQLAlchemy or auth is not available, continue without auth
    pass
# --- end auth init ---
# --- Initialize Firebase ---
try:
    firebase_config.init_firebase()
    logger.info("Firebase initialized")
    
    # Initialize Firestore sync (must be after db.create_all() and Firebase init)
    try:
        from firestore_sync import init_firestore_sync
        init_firestore_sync(app)
        logger.info("Firestore sync initialized")
    except ImportError:
        logger.debug("Firestore sync module not available")
    except Exception as e:
        logger.warning(f"Firestore sync initialization failed: {e}")
    
    # Register Firebase Auth routes
    from firebase_auth_routes import firebase_auth_bp
    app.register_blueprint(firebase_auth_bp)
    logger.info("Firebase Auth routes registered")
    
    # Register Admin API routes
    from admin_api import admin_api_bp
    app.register_blueprint(admin_api_bp)
    logger.info("Admin API routes registered")
    # Start periodic background sync of local data/ to Firebase (encrypted)
    try:
        import firebase_config as _fc
        _fc.start_firebase_data_sync()
    except Exception:
        logger.exception('Could not start firebase data sync')
except Exception as e:
    logger.warning(f"Firebase initialization failed: {e}")
# --- end firebase init ---
try:
    # If Flask-Login is available, enforce login for non-auth endpoints
    from flask_login import current_user
    @app.before_request
    def require_login_before_request():
        # allow disabling login checks in tests (honour Flask-Login convention)
        try:
            if current_app.config.get('LOGIN_DISABLED'):
                return None
        except Exception:
            pass

        # allow static, local auth and firebase auth endpoints
        endpoint = request.endpoint or ''
        if endpoint.startswith('auth.') or endpoint.startswith('static') or endpoint.startswith('firebase_auth.'):
            return None
        # allow public pages (terms, privacy, cookie consent)
        if endpoint in ['terms_page', 'privacy_page', '_debug_log']:
            return None
        # allow public API endpoints (if any) - keep a whitelist here if needed
        public = {'home', 'index', 'healthcheck', 'serve_icons'}
        remote_public_endpoints = {
            'mobile_qr_scanner',
            'api_qr_remote_attach',
            'api_qr_remote_heartbeat',
            'api_qr_remote_update',
            'api_qr_remote_push',
            'api_qr_remote_control',
        }
        if endpoint in public:
            return None
        if endpoint in remote_public_endpoints:
            return None

        # Allow Resend inbound webhooks to hit this endpoint without a login session.
        # Authentication is handled via the Svix signature verification in the webhook handler.
        if request.path.startswith('/admin/api/resend/inbound'):
            return None

        try:
            if not getattr(current_user, 'is_authenticated', False):
                # API requests: return 401 JSON
                if request.path.startswith('/api/') or request.is_json:
                    return jsonify({'error': 'authentication required'}), 401
                return redirect(url_for('auth.login', next=request.path))
        except Exception:
            # if anything goes wrong, do not block app startup
            return None
except Exception:
    pass
FISCAL_META = 'fiscal.meta.json'   # αποθηκεύεται μέσα στο DATA_DIR
REQUIRED_CLIENT_COLUMNS = {"ΑΦΜ", "Επωνυμία", "Διεύθυνση", "Πόλη", "ΤΚ", "Τηλέφωνο"}  # προσάρμοσε αν χρειάζεται
CREDENTIALS_PATH = os.path.join(DATA_DIR, "credentials.json")


def get_group_base_dir():
    """Return absolute path to the data directory for the currently active group (or user's single group).
    Falls back to the global DATA_DIR if no group selected or available.

    This helper now also honours a *thread-local override* set by
    background tasks such as the fetch worker.  Without the override the
    worker would call ``get_active_group()`` outside of a request
    context, obtain ``None`` and therefore end up writing into the
    global ``DATA_DIR``.  The override keeps the correct folder even if
    the request context has disappeared.
    """
    # check for thread-local override first (background threads)
    override = _get_thread_group_base_dir()
    if override:
        return override

    try:
        # avoid top-level import cycles
        from auth import get_active_group
        grp = get_active_group()
    except Exception:
        grp = None

    if grp and getattr(grp, 'data_folder', None):
        base = os.path.join(BASE_DIR, 'data', grp.data_folder)
    else:
        base = DATA_DIR

    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        pass
    return base


def credentials_path_for_request():
    return os.path.join(get_group_base_dir(), 'credentials.json')


@app.route('/api/sync_progress', methods=['GET'])
@monitor_resources('api_sync_progress')
def api_sync_progress():
    """Return current sync progress for the active group (non-blocking)."""
    try:
        from auth import get_active_group
        grp = get_active_group()
        if not grp or not getattr(grp, 'data_folder', None):
            return jsonify({'status': 'no_group', 'percent': 0, 'message': 'No active group'})
        # If Firebase is not enabled, report explicitly so frontend won't show overlay
        try:
            if not firebase_config.is_firebase_enabled():
                return jsonify({'status': 'disabled', 'percent': 0, 'message': 'Firebase disabled'})
        except Exception:
            pass

        prog = None
        try:
            prog = firebase_config.get_group_sync_progress(grp.data_folder)
        except Exception:
            prog = {'status': 'not_started', 'percent': 0, 'message': ''}
        return jsonify(prog)
    except Exception as e:
        app.logger.error(f"Failed to get sync progress: {e}")
        return jsonify({'status': 'error', 'percent': 0, 'message': str(e)}), 500


@app.route('/api/debug/role', methods=['GET'])
@login_required
@monitor_resources('api_debug_role')
def api_debug_role():
    """Debug endpoint to check user role detection in active group."""
    try:
        from auth import get_active_group
        from flask_login import current_user
        
        result = {
            'user_id': current_user.id if current_user else None,
            'username': current_user.username if current_user else None,
            'is_authenticated': getattr(current_user, 'is_authenticated', False),
            'session_active_group': session.get('active_group'),
            'active_group': None,
            'role_in_group': None,
            'all_user_groups': [],
            'errors': []
        }
        
        # Get active group
        grp = get_active_group()
        if grp:
            result['active_group'] = {
                'id': grp.id,
                'name': grp.name
            }
            # Get role
            try:
                role = current_user.role_for_group(grp)
                result['role_in_group'] = role
            except Exception as e:
                result['errors'].append(f"role_for_group error: {e}")
        else:
            result['errors'].append("get_active_group returned None")
        
        # List all user groups
        try:
            for g in current_user.groups:
                result['all_user_groups'].append({
                    'id': g.id,
                    'name': g.name,
                    'role': current_user.role_for_group(g) if g else None
                })
        except Exception as e:
            result['errors'].append(f"user.groups error: {e}")
        
        return jsonify(result), 200
    except Exception as e:
        app.logger.exception(f"Debug role endpoint error: {e}")
        return jsonify({'error': str(e)}), 500

def _load_credentials():
    p = Path(credentials_path_for_request())
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Δέξου είτε λίστα είτε μοναδικό αντικείμενο
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception as e:
        app.logger.warning("Failed to load credentials.json: %s", e)
        return []

def _save_credentials(items):
    p = Path(credentials_path_for_request())
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

def _find_client(creds, vat=None, name=None):
    if vat:
        for c in creds:
            if str(c.get("vat","")).strip() == str(vat).strip():
                return c
    if name:
        for c in creds:
            if str(c.get("name","")).strip() == str(name).strip():
                return c
    # Fallback: if session has an active credential, prefer that
    try:
        from flask import session
        active = session.get('active_credential')
        if active:
            for c in creds:
                if str(c.get('name') or '').strip() == str(active).strip() or str(c.get('vat') or '').strip() == str(active).strip():
                    return c
    except Exception:
        pass
    # do not return a default credential when no match is found; callers
    # will handle a None appropriately (e.g. falling back to active session
    # object).  Returning the first item caused mismatches in multi-client
    # tests.
    return None

def _load_all_credentials():
    try:
        p = Path(credentials_path_for_request())
        with p.open("r", encoding="utf-8") as f:
            return json.load(f) or []
    except FileNotFoundError:
        return []
    except Exception:
        return []

def _save_all_credentials(creds):
    base = get_group_base_dir()
    os.makedirs(base, exist_ok=True)
    p = Path(os.path.join(base, 'credentials.json'))
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)

def _active_cred_index(creds):
    active = get_active_credential_from_session() or {}
    vat = (active.get("vat") or "").strip()
    name = (active.get("name") or "").strip()
    for i,c in enumerate(creds):
        if vat and str(c.get("vat") or "").strip() == vat:
            return i
    for i,c in enumerate(creds):
        if name and str(c.get("name") or "").strip() == name:
            return i
    return 0 if creds else -1

def _profiles_get_for_active():
    creds = _load_all_credentials()
    idx = _active_cred_index(creds)
    if idx < 0:
        return [], []
    c = creds[idx]
    c.setdefault("char_profiles", [])  # [{id,name,map}]
    tags = _list_invoice_categories(c)
    return c["char_profiles"], tags


# ---------------- Per-group path helpers ----------------
def group_path(*parts: str) -> str:
    """Return an absolute path inside the active group's data folder (or global DATA_DIR fallback)."""
    try:
        base = get_group_base_dir()
    except Exception:
        base = DATA_DIR
    if not parts:
        return base
    return os.path.join(base, *parts)


def invoices_cache_path() -> str:
    return group_path('invoices_cache.json')


def summary_path() -> str:
    return group_path('summary.json')


def error_log_path() -> str:
    return group_path('error.log')


def activity_log_path() -> str:
    return group_path('activity.log')


def epsilon_json_path() -> str:
    return group_path('epsilon_invoices.json')


def epsilon_excel_path() -> str:
    return group_path('epsilon_invoices.xlsx')


def mark_counter_path() -> str:
    return group_path('mark_counter.json')


def settings_file_path() -> str:
    return group_path('credentials_settings.json')


def _profiles_set_for_active(profiles):
    creds = _load_all_credentials()
    idx = _active_cred_index(creds)
    if idx < 0:
        return False
    creds[idx]["char_profiles"] = profiles
    _save_all_credentials(creds)
    return True


@app.before_request
def log_request_path():
    label = f"{request.method} {request.path}"
    start_request_monitoring(label)
    log.info("Incoming request: method=%s path=%s remote=%s ref=%s", request.method, request.path, request.remote_addr, request.referrer)


@app.after_request
def log_request_complete(response):
    # Capture status and end timing for every request
    end_request_monitoring(status_code=response.status_code)
    return response


@app.before_request
def session_heartbeat():
    """Lightweight heartbeat to update user's last_active_at when they have an active session.
    Writes to DB at most once every 30 seconds (tracked in flask session) to avoid excessive writes.
    """
    try:
        from flask_login import current_user
        from flask import session as _session
        from models import db as _db
        # Ignore automatic background polling endpoints; they should not count as user activity.
        path = (request.path or '')
        if path in ('/api/global_notifications', '/api/fetch_progress', '/api/last_fetch_date'):
            return None
        if not getattr(current_user, 'is_authenticated', False):
            return None
        sid = _session.get('session_id')
        if not sid:
            return None
        # Only heartbeat if session_id matches user's claimed session
        if getattr(current_user, 'current_session_id', None) != sid:
            return None
        # Throttle DB writes: only update if last heartbeat older than 30s
        try:
            last_ts = _session.get('_last_heartbeat_ts')
            now = datetime.datetime.utcnow()
            do_update = False
            if not last_ts:
                do_update = True
            else:
                try:
                    last_dt = datetime.datetime.fromisoformat(str(last_ts))
                    if (now - last_dt).total_seconds() > 30:
                        do_update = True
                except Exception:
                    do_update = True
            if do_update:
                current_user.last_active_at = now
                _db.session.commit()
                _session['_last_heartbeat_ts'] = now.isoformat()
        except Exception:
            try:
                _db.session.rollback()
            except Exception:
                pass
    except Exception:
        # fail silently
        pass


@app.before_request
def enforce_active_session_claim():
    """Enforce single active session per user and inactivity timeout (default 30')."""
    try:
        from flask_login import current_user, logout_user
        if not getattr(current_user, 'is_authenticated', False):
            return None

        path = (request.path or "")
        if path.startswith('/static/') or path in ('/auth/login', '/auth/api/logout', '/logout'):
            return None

        sid = session.get('session_id')
        claimed_sid = getattr(current_user, 'current_session_id', None)

        # Missing or mismatched claim means user logged in from another browser/device.
        if not sid or not claimed_sid or str(sid) != str(claimed_sid):
            try:
                logout_user()
            except Exception:
                pass
            for key in ('active_credential', '_remote_qr_owner', 'session_id', '_last_heartbeat_ts'):
                session.pop(key, None)
            if path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'session_conflict', 'message': 'Εντοπίστηκε σύνδεση από άλλη συσκευή.'}), 401
            return redirect(url_for('auth.login', session_conflict='1'))

        timeout_seconds = int(current_app.config.get('SESSION_TIMEOUT_SECONDS', 900))
        now = datetime.datetime.utcnow()
        last_active = getattr(current_user, 'last_active_at', None)
        if last_active and (now - last_active).total_seconds() > timeout_seconds:
            try:
                from models import db as _db
                current_user.end_session(sid)
                _db.session.commit()
            except Exception:
                try:
                    _db.session.rollback()
                except Exception:
                    pass
            try:
                logout_user()
            except Exception:
                pass
            for key in ('active_credential', '_remote_qr_owner', 'session_id', '_last_heartbeat_ts'):
                session.pop(key, None)
            if path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'session_expired', 'message': 'Η συνεδρία έληξε λόγω αδράνειας.'}), 401
            return redirect(url_for('auth.login', session_expired='1'))
    except Exception:
        return None
# --- Logging (Europe/Athens timezone) ---
import datetime
import logging
from logging.handlers import RotatingFileHandler

# Προσπάθεια για DST-aware ζώνη ώρας Ελλάδας
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
    GREECE_TZ = ZoneInfo("Europe/Athens")
except Exception:
    try:
        import pytz  # fallback αν λείπει zoneinfo
        GREECE_TZ = pytz.timezone("Europe/Athens")
    except Exception:
        # έσχατο fallback: σταθερό UTC+3 (χωρίς DST)
        GREECE_TZ = datetime.timezone(datetime.timedelta(hours=3))

class GreeceTZFormatter(logging.Formatter):
    """Formatter που τυπώνει timestamps σε Europe/Athens."""
    def __init__(self, fmt=None, datefmt=None, tz=GREECE_TZ):
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.tz = tz

    def formatTime(self, record, datefmt=None):
        # Χτίζουμε timezone-aware datetime από το record.created
        dt = datetime.datetime.fromtimestamp(record.created, self.tz)
        if datefmt:
            return dt.strftime(datefmt)
        # default: ISO-like χωρίς milliseconds
        return dt.isoformat(timespec="seconds")

log = logging.getLogger("mydata_app")


def delete_customer_data_files(vat: str) -> Dict[str, Any]:
    """Remove JSON/Excel artifacts for a specific VAT from the *active group*.

    Prior to the multi-group refactor this walked ``DATA_DIR``; that meant
    when a user switched teams the cleanup command would erase files from
    other groups too.  The new implementation restricts the walk to the
    folder returned by :func:`group_path` (which already handles the
    global fallback when no group is selected).

    Returns a dict with keys:
      - count: number of files removed
      - files: absolute paths removed
      - failed: list of {'path', 'error'} for files that could not be removed
    """

    vat = str(vat or "").strip()
    result: Dict[str, Any] = {"count": 0, "files": [], "failed": []}
    if not vat:
        return result

    tokens = {vat}
    digits_only = re.sub(r"\D", "", vat)
    if digits_only:
        tokens.add(digits_only)

    allowed_ext = {".json", ".xlsx", ".xls", ".csv"}
    # walk only the active group's data folder (or DATA_DIR fallback)
    base_dir = os.path.abspath(group_path())

    for root, _, files in os.walk(base_dir):
        for fname in files:
            _, ext = os.path.splitext(fname)
            if ext.lower() not in allowed_ext:
                continue
            if not any(token and token in fname for token in tokens):
                continue
            path = os.path.join(root, fname)
            try:
                os.remove(path)
                result["count"] += 1
                result["files"].append(path)
            except FileNotFoundError:
                continue
            except Exception as exc:  # pragma: no cover - safeguard logging
                log.warning("Failed to remove data file %s for VAT %s: %s", path, vat, exc)
                result["failed"].append({"path": path, "error": str(exc)})

    return result
log.setLevel(logging.INFO)

if not log.handlers:
    fmt = "%(asctime)s %(levelname)s %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S%z"

    # Console
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(GreeceTZFormatter(fmt=fmt, datefmt=datefmt, tz=GREECE_TZ))
    log.addHandler(sh)

    # Activity log (όλες οι κινήσεις)
    ah = RotatingFileHandler(ACTIVITY_LOG, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    ah.setLevel(logging.INFO)
    ah.setFormatter(GreeceTZFormatter(fmt=fmt, datefmt=datefmt, tz=GREECE_TZ))
    log.addHandler(ah)

    # Error log (προβλήματα/προειδοποιήσεις)
    fh = RotatingFileHandler(ERROR_LOG, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.WARNING)
    fh.setFormatter(GreeceTZFormatter(fmt=fmt, datefmt=datefmt, tz=GREECE_TZ))
    log.addHandler(fh)

# (προαιρετικά) συντόνισε και τον werkzeug logger να γράφει με ασφαλή formatter.
# Σε ορισμένα περιβάλλοντα (π.χ. Python 3.14) ο default handler chain μπορεί
# να πετάει logging-format exceptions σε request logs.
try:
    wlog = logging.getLogger("werkzeug")
    wlog.setLevel(logging.INFO)
    wlog.propagate = False

    for existing in list(wlog.handlers):
        try:
            wlog.removeHandler(existing)
        except Exception:
            pass

    wz_fmt = "%(asctime)s %(levelname)s %(message)s"
    wz_datefmt = "%Y-%m-%d %H:%M:%S%z"
    wsh = logging.StreamHandler(sys.stdout)
    wsh.setLevel(logging.INFO)
    wsh.setFormatter(GreeceTZFormatter(fmt=wz_fmt, datefmt=wz_datefmt, tz=GREECE_TZ))
    wlog.addHandler(wsh)
except Exception:
    pass

log.info("Starting app - MYDATA_ENV=%s", MYDATA_ENV)

# Admin configuration
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
# expose to Flask app config so decorators and blueprints can read it
try:
    app.config.setdefault('ADMIN_USER_ID', ADMIN_USER_ID)
except Exception:
    # app may not be defined yet in some import orders; ignore if so
    pass

GLOBAL_ACCOUNTS_NAME = "__global_accounts__"
# keep settings inside data/ so it's co-located with other app files
SETTINGS_FILE = os.path.join(DATA_DIR, "credentials_settings.json")
VAT_MAP = {
    "1": "ΦΠΑ 24%",
    "2": "ΦΠΑ 13%",
    "3": "ΦΠΑ 6%",
    "4": "ΦΠΑ 17%",
    "5": "ΦΠΑ 9%",
    "6": "ΦΠΑ 4%",
    "7": "Άνευ ΦΠΑ",
    "8": "Εξαιρούμενο άρθρο 39α",
    "9": "Εξαιρούμενο άρθρο 47β",
}
# ==== app.py (HEAD) ====
from pathlib import Path
import os, json, uuid, datetime

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CRED_PATH = ROOT_DIR / "data" / "credentials.json"
CREDENTIALS_PATH = Path(os.environ.get("CREDENTIALS_PATH", DEFAULT_CRED_PATH))

VAT_KEYS = ["0%", "6%", "13%", "17%", "24%"]
VAT_RATE_NUMERIC = ["0", "3", "4", "6", "9", "13", "17", "24"]


def _normalize_repeat_mapping(raw: Optional[Dict[str, Any]]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    source = raw or {}
    for key in VAT_KEYS:
        mapping[key] = str(source.get(key) or "").strip()
    return mapping


def _is_complete_repeat_mapping(mapping: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(mapping, dict):
        return False
    return all((str(mapping.get(key) or "").strip()) for key in VAT_KEYS)


def _build_repeat_entry_payload(repeat: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = dict(repeat or {})
    mapping_raw = payload.get("mapping") or {}
    mapping = {k: v for k, v in mapping_raw.items() if k in VAT_KEYS and (v or "").strip()}
    general_raw = payload.get("general_mapping") or {}
    general_mapping = {k: v for k, v in general_raw.items() if k in VAT_KEYS and (v or "").strip()}
    if not general_mapping:
        general_mapping = dict(mapping)
    payload["mapping"] = mapping
    payload["general_mapping"] = general_mapping
    return payload

SERIES_SETTING_KEYS = [
    "invoice_general",
    "invoice_services",
    "invoice_credit",
    "receipt_retail",
    "receipt_services",
    "receipt_credit",
]

SERIES_INVOICE_DEFAULT = "invoice_general"
SERIES_RECEIPT_DEFAULT = "receipt_retail"

SERIES_FALLBACK_MAP = {
    "invoice_services": SERIES_INVOICE_DEFAULT,
    "invoice_credit": SERIES_INVOICE_DEFAULT,
    "receipt_services": SERIES_RECEIPT_DEFAULT,
    "receipt_credit": SERIES_RECEIPT_DEFAULT,
}

DEFAULT_INVOICE_CATEGORY_LABELS: Dict[str, str] = {
    "αγορες_εμπορευματων": "Αγορές εμπορευμάτων",
    "αγορες_α_υλων": "Αγορές α' υλών",
    "γενικες_δαπανες": "Γενικές δαπάνες",
    "γενικες_δαπανες_με_φπα": "Γενικές δαπάνες με ΦΠΑ",
    "αμοιβες_τριτων": "Αμοιβές και έξοδα τρίτων",
    "δαπανες_χωρις_φπα": "Δαπάνες χωρίς δικαίωμα έκπτωσης ΦΠΑ",
    "εγγυοδοσια": "Εγγυοδοσία",
    "αποδειξακια": "Αποδειξάκια",
}


def _ensure_custom_categories_list(client: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(client, dict):
        return []
    arr = client.get("custom_categories")
    if isinstance(arr, list):
        return arr
    client["custom_categories"] = []
    return client["custom_categories"]


def _custom_category_receipts_enabled(item: Optional[Dict[str, Any]]) -> bool:
    """Return True if the custom category is to be treated as available for receipts.

    The priority order is:
    1. explicit boolean flag (applies_to_receipts / receipt_enabled / receipts_enabled).
       **If any such flag is present and False, the category is considered
       *not* receipt-enabled, even if accounts are filled.** This allows the
       user to deselect the checkbox and override any existing account codes.
    2. metadata inside the ``accounts`` dictionary (``__applies_to_receipts``
       or ``receipt_enabled``) when the explicit flag is absent.
    3. if no flag at all was provided, the presence of any non‑empty account
       code is treated as an implicit signal that the category should be
       available for receipts.  This keeps the system compatible with older
       data.
    """
    if not isinstance(item, dict):
        return False

    # explicit boolean flags first
    raw = item.get("applies_to_receipts")
    if raw is None:
        raw = item.get("receipt_enabled")
    if raw is None:
        raw = item.get("receipts_enabled")

    # fall back to metadata inside accounts if still undetermined
    accounts = item.get("accounts") if isinstance(item.get("accounts"), dict) else {}
    if raw is None:
        raw = (
            accounts.get("__applies_to_receipts")
            if "__applies_to_receipts" in accounts
            else accounts.get("receipt_enabled")
        )

    # if there was an explicit false, respect it and do not inspect accounts
    if raw is False:
        return False

    if raw:
        return True

    # finally, if the category has any non-empty account codes we treat it
    # as receipt-capable (user has effectively configured it)
    try:
        from . import _normalize_custom_accounts
    except ImportError:
        # should never happen but be safe
        _normalize_custom_accounts = lambda x: x or {}
    norm = _normalize_custom_accounts(accounts)
    for code in norm.values():
        if code and str(code).strip():
            return True
    return False


def _category_labels_for_client(client: Optional[Dict[str, Any]]) -> Dict[str, str]:
    labels = dict(DEFAULT_INVOICE_CATEGORY_LABELS)
    if not isinstance(client, dict):
        return labels
    for item in _ensure_custom_categories_list(client):
        slug = str(item.get("id") or item.get("slug") or "").strip()
        if not slug:
            continue
        label = str(item.get("label") or slug).strip()
        if label:
            labels[slug] = label
    return labels


def _list_invoice_categories(client: Optional[Dict[str, Any]], include_receipts: bool = False) -> List[str]:
    if not isinstance(client, dict):
        return []
    out: List[str] = []
    for tag in client.get("expense_tags") or []:
        if not tag:
            continue
        tag_str = str(tag).strip()
        if not tag_str:
            continue
        if not include_receipts and tag_str.lower() == "αποδειξακια":
            continue
        if tag_str not in out:
            out.append(tag_str)
    for item in _ensure_custom_categories_list(client):
        if not item or not item.get("enabled"):
            continue
        slug = str(item.get("id") or item.get("slug") or "").strip()
        if slug and slug not in out:
            out.append(slug)
    return out


def _list_receipt_categories(client: Optional[Dict[str, Any]]) -> List[str]:
    """Return categories usable for receipts.

    Keep the default "αποδειξακια" tag and append only custom categories
    that are explicitly flagged for receipts.
    """
    if not isinstance(client, dict):
        return ["αποδειξακια"]
    out: List[str] = ["αποδειξακια"]

    def _explicit_receipts_enabled(item: Optional[Dict[str, Any]]) -> bool:
        if not isinstance(item, dict):
            return False
        raw = item.get("applies_to_receipts")
        if raw is None:
            raw = item.get("receipt_enabled")
        if raw is None:
            raw = item.get("receipts_enabled")
        accounts = item.get("accounts") if isinstance(item.get("accounts"), dict) else {}
        if raw is None and isinstance(accounts, dict):
            raw = accounts.get("__applies_to_receipts") if "__applies_to_receipts" in accounts else accounts.get("receipt_enabled")
        return bool(raw)
    # append receipt-enabled custom categories
    for item in _ensure_custom_categories_list(client):
        if not item:
            continue
        if not _explicit_receipts_enabled(item):
            continue
        slug = str(item.get("id") or item.get("slug") or "").strip()
        if slug and slug not in out:
            out.append(slug)
    if "αποδειξακια" not in out:
        out.insert(0, "αποδειξακια")
    return out


def _slugify_custom_category(label: str, existing: Optional[set[str]] = None) -> str:
    existing = existing or set()
    base = _normalize(label or "")
    base = base.replace(" ", "_")
    base = re.sub(r"[^a-z0-9_]+", "", base)
    base = re.sub(r"_+", "_", base).strip("_")
    if not base:
        base = "custom"
    if not base.startswith("custom_"):
        base = f"custom_{base}"
    candidate = base
    counter = 2
    while candidate in existing:
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def _normalize_custom_accounts(accounts: Optional[Dict[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    accounts = accounts or {}
    for rate in VAT_RATE_NUMERIC:
        raw = accounts.get(rate)
        val = str(raw).strip() if isinstance(raw, (str, int, float)) else ""
        out[rate] = val
    return out


def _allowed_vat_keys_for_category(item: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(item, dict):
        return []
    accounts = _normalize_custom_accounts(item.get("accounts") if isinstance(item, dict) else {})
    allowed: List[str] = []
    for rate in VAT_RATE_NUMERIC:
        code = accounts.get(rate, "")
        if code:
            allowed.append(f"{rate}%")
    return allowed


def _category_vat_constraints(client: Optional[Dict[str, Any]], book_category: Optional[str] = None) -> Dict[str, List[str]]:
    constraints: Dict[str, List[str]] = {}
    if not isinstance(client, dict):
        return constraints

    category_raw = str(book_category if book_category is not None else client.get("book_category") or "Β").strip().upper()
    is_g_category = category_raw in ("Γ", "G")

    # 1) Built-in expense tags: derive allowed VAT keys from global accounts and settings
    try:
        global_accounts = get_global_accounts_from_credentials() or {}
    except Exception:
        global_accounts = {}

    try:
        settings = load_settings() or {}
    except Exception:
        settings = {}

    # build a rate -> { tag: code } map from settings keys
    settings_accounts: Dict[str, Dict[str, str]] = {}
    for key, val in (settings or {}).items():
        if not key.startswith('account_'):
            continue
        k = key[len('account_'):]
        has_g_prefix = k.startswith('g_')
        if is_g_category:
            if not has_g_prefix:
                continue
            k = k[2:]
        else:
            if has_g_prefix:
                continue
        m = re.match(r'(?P<tag>.+?)_fpa_kat_(?P<rate>\d+)%$', k)
        if not m:
            m2 = re.match(r'(?P<tag>.+?)_(?P<rate>\d+)%$', k)
            if m2:
                tag = m2.group('tag')
                rate = m2.group('rate')
            else:
                continue
        else:
            tag = m.group('tag')
            rate = m.group('rate')
        code = (val or '').strip()
        if not code:
            continue
        tag_norm = str(tag).strip()
        if tag_norm.endswith('_με_ΦΠΑ'):
            tag_base = tag_norm[:-len('_με_ΦΠΑ')]
        else:
            tag_base = tag_norm
        settings_accounts.setdefault(rate, {})[tag_norm] = code
        if tag_base != tag_norm:
            settings_accounts.setdefault(rate, {})[tag_base] = code

    # merge settings_accounts into global_accounts shape
    try:
        if isinstance(global_accounts, dict):
            for rate, tagsmap in settings_accounts.items():
                if not tagsmap:
                    continue
                if isinstance(global_accounts.get(rate), dict):
                    global_accounts[rate].update(tagsmap)
                else:
                    if isinstance(global_accounts.get(f"{rate}%"), dict):
                        global_accounts[f"{rate}%"].update(tagsmap)
                    else:
                        global_accounts.setdefault(str(rate), {}).update(tagsmap)
    except Exception:
        pass

    expense_tags = None
    try:
        # include_receipts=True so constraints are also computed for the
        # special receipts category "αποδειξακια" when accounts exist.
        expense_tags = _list_invoice_categories(client, include_receipts=True)
    except Exception:
        expense_tags = client.get('expense_tags') or []

    for tag in expense_tags or []:
        if not tag:
            continue
        tag_str = str(tag).strip()
        if not tag_str:
            continue
        allowed: List[str] = []
        for rate in VAT_RATE_NUMERIC:
            found_for_rate = False
            for gk, vat_bucket in (global_accounts.items() if isinstance(global_accounts, dict) else []):
                try:
                    if not gk:
                        continue
                    key_norm = str(gk).strip()
                    key_norm = key_norm[:-1] if key_norm.endswith('%') else key_norm
                    if key_norm == str(rate):
                        if isinstance(vat_bucket, dict) and vat_bucket.get(tag_str):
                            allowed.append(f"{rate}%")
                            found_for_rate = True
                            break
                except Exception:
                    continue
            if found_for_rate:
                continue
        if allowed:
            constraints[tag_str] = allowed

    # 2) Custom categories: per-category accounts stored on the client object
    for item in _ensure_custom_categories_list(client):
        slug = str(item.get("id") or item.get("slug") or "").strip()
        if not slug:
            continue
        allowed = _allowed_vat_keys_for_category(item)
        if allowed:
            constraints[slug] = allowed
    return constraints


def _merge_settings_with_custom(settings: Dict[str, Any], client: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = dict(settings or {})
    if not isinstance(client, dict):
        return merged
    for cat in _ensure_custom_categories_list(client):
        slug = str(cat.get("id") or cat.get("slug") or "").strip()
        if not slug:
            continue
        accounts = _normalize_custom_accounts(cat.get("accounts") if isinstance(cat, dict) else {})
        for rate, code in accounts.items():
            if not code:
                continue
            key = f"account_{slug}_fpa_kat_{rate}%"
            merged[key] = code
    return merged


def _custom_categories_payload(client: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not isinstance(client, dict):
        return []
    payload: List[Dict[str, Any]] = []
    for item in _ensure_custom_categories_list(client):
        slug = str(item.get("id") or item.get("slug") or "").strip()
        if not slug:
            continue
        accounts = _normalize_custom_accounts(item.get("accounts") if isinstance(item, dict) else {})
        payload.append({
            "id": slug,
            "label": str(item.get("label") or slug),
            "enabled": bool(item.get("enabled")),
            "applies_to_receipts": _custom_category_receipts_enabled(item),
            "accounts": accounts,
            "allowed_vat_keys": _allowed_vat_keys_for_category(item),
        })
    return payload
def _resolve_client_db_path(vat: str) -> str | None:
    """
    Επιστρέφει per-group διαδρομή για client_db.* με έξυπνα fallbacks.
    Προτεραιότητα: data/<group>/... -> global data/.
    Δεκτά: .xlsx/.xls/.csv και per-VAT ονομασίες.
    """
    vat = str(vat or "").strip()
    base = get_group_base_dir()  # π.χ. .../data/<group>

    # 1) Κοίτα πρώτα στον φάκελο της ομάδας
    candidates = [
        os.path.join(base, f"client_db_{vat}.xlsx"),
        os.path.join(base, f"{vat}_client_db.xlsx"),
        os.path.join(base, "client_db.xlsx"),
        os.path.join(base, f"client_db_{vat}.xls"),
        os.path.join(base, f"{vat}_client_db.xls"),
        os.path.join(base, "client_db.xls"),
        os.path.join(base, "client_db.csv"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p

    # 2) Fallback: «έξυπνη» ανακάλυψη στον φάκελο της ομάδας
    try:
        from epsilon_bridge_multiclient_strict import _discover_client_db_in_data_dir
        fb = _discover_client_db_in_data_dir(base, vat=vat)
        if fb:
            return fb
    except Exception:
        pass

    # 3) Fallback: «έξυπνη» ανακάλυψη στο global data/
    try:
        from epsilon_bridge_multiclient_strict import _discover_client_db_in_data_dir
        fb = _discover_client_db_in_data_dir(os.path.join(BASE_DIR, "data"), vat=vat)
        if fb:
            return fb
    except Exception:
        pass

    return None


def _normalize(s: str) -> str:
    """
    Κανονικοποίηση string: lower, χωρίς τόνους/διακριτικά, trim.
    """
    s = str(s or "").strip()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s.lower()

def _canon_afm(val) -> str:
    """
    Επιστρέφει AFM μόνο με ψηφία (αφαιρεί διαχωριστικά, κενά, κλπ).
    Αν δεν βρεθούν ψηφία, επιστρέφει trimmed string.
    """
    txt = str(val or "").strip()
    digits = re.sub(r"\D+", "", txt)
    return digits or txt

# Υποψήφιοι τίτλοι στηλών για AFM & ID (σε normalized μορφή)
_AFM_HEADER_CANDIDATES = {
    "afm", "αφμ", "vat", "vat no", "vat number", "a.f.m", "a.f.m.", "tax id", "taxid"
}
_ID_HEADER_CANDIDATES = {
    # Πολλές γραφές για 'Κωδ. Συναλλασσόμενου'
    "κωδ. συναλλασσομενου", "κωδ συναλλασσομενου", "κωδικος συναλλασσομενου",
    "κωδικος", "κωδ.", "κωδ", "id", "customer id", "account id",
    "κωδ. πελατη", "κωδ πελατη", "κωδικος πελατη",
    "κωδ. προμηθευτη", "κωδικος προμηθευτη",
}

def _read_first_sheet_anyname(path: str) -> pd.DataFrame:
    """
    Διαβάζει ΠΑΝΤΑ το 1ο sheet (index 0), ανεξαρτήτως ονόματος.
    Υποστηρίζει .xls (xlrd==1.2.0) και .xlsx (openpyxl).
    """
    if not path:
        return pd.DataFrame()
    ext = os.path.splitext(path)[1].lower()
    try:
        engine = "xlrd" if ext == ".xls" else "openpyxl"
        xls = pd.ExcelFile(path, engine=engine)
        first_name = xls.sheet_names[0]
        df = xls.parse(first_name)
        current_app.logger.info(
            "client_db: using first sheet name=%r rows=%d", first_name, len(df)
        )
        return df
    except Exception as e:
        current_app.logger.exception("Failed to read first sheet from '%s': %s", path, e)
        # ύστατο fallback: read_excel (παίρνει 1ο sheet by default)
        try:
            df = pd.read_excel(path)
            current_app.logger.warning("client_db: fallback read_excel() rows=%d", len(df))
            return df
        except Exception as e2:
            current_app.logger.exception("Fallback read_excel failed for '%s': %s", path, e2)
            return pd.DataFrame()

def _detect_cols(df: pd.DataFrame) -> tuple[str | None, str | None]:
    """
    Εντοπίζει ονόματα στηλών για AFM και CUSTID.
    - Ανίχνευση με απόλυτη ταύτιση candidate sets
    - ή με patterns ('συναλλασ' & 'κωδ' για ID, 'afm/αφμ/vat' για AFM)
    """
    afm_col, id_col = None, None
    for c in list(df.columns):
        lc = _normalize(c)
        if afm_col is None and (
            lc in _AFM_HEADER_CANDIDATES or "αφμ" in lc or "vat" in lc or "afm" in lc
        ):
            afm_col = c
        if id_col is None and (
            lc in _ID_HEADER_CANDIDATES or ("συναλλασ" in lc and ("κωδ" in lc or "κωδικ"))
        ):
            id_col = c
    return afm_col, id_col

def _coerce_id(val):
    """
    Μετατρέπει το ID σε integer όταν είναι "170", "170.0", κ.λπ.·
    αλλιώς το επιστρέφει ως string (ώστε να διατηρούνται τυχόν leading zeros).
    """
    if pd.isna(val):
        return None
    s = str(val).strip()
    if s == "":
        return None
    # αν είναι float-μορφής ακέραιος (π.χ. 170.0) → int
    try:
        f = float(s)
        if f.is_integer():
            return int(f)
    except Exception:
        pass
    return s

APP_DIR = os.path.abspath(os.path.dirname(__file__))
if APP_DIR not in sys.path:
    sys.path.append(APP_DIR)


def _enrich_issuer_name_from_afm(vat: str, issuer_afm: str, existing_name: str = None) -> Optional[str]:
    """
    Εμπλουτισμός ονόματος εκδότη με βάση το ΑΦΜ του.
    Ψάχνει πρώτα στο client_db του group, αν δεν βρει ψάχνει με VAT validator.
    
    Args:
        vat: ΑΦΜ ενεργού πελάτη (για εύρεση client_db)
        issuer_afm: ΑΦΜ εκδότη που θέλουμε να βρούμε το όνομά του
        existing_name: Υπάρχον όνομα (αν υπάρχει) - αν έχει τιμή, επιστρέφει αμέσως
    
    Returns:
        Το όνομα του εκδότη αν βρεθεί, αλλιώς None
    """
    # Αν υπάρχει ήδη όνομα, δεν κάνουμε τίποτα
    if existing_name and str(existing_name).strip():
        return existing_name
    
    if not issuer_afm or not str(issuer_afm).strip():
        return None
    
    # 1) Ψάχνουμε στο client_db του group
    try:
        from epsilon_bridge_multiclient_strict import _load_client_map, _norm_afm as bridge_norm_afm
        
        # Χρησιμοποιούμε την ίδια κανονικοποίηση που χρησιμοποιεί το _load_client_map
        issuer_afm_clean = bridge_norm_afm(issuer_afm)
        
        if not issuer_afm_clean:
            log.debug(f"Invalid issuer AFM format: {issuer_afm}")
            return None
        
        # Βρες το client_db path (ψάχνει πρώτα στο group)
        client_db_path = _resolve_client_db_path(vat)
        
        if client_db_path and os.path.exists(client_db_path):
            try:
                log.info(f"Loading client_db from: {client_db_path}")
                client_map = _load_client_map(client_db_path)
                
                # Αναζήτηση στο names dictionary
                if issuer_afm_clean in client_map.get("names", {}):
                    found_name = client_map["names"][issuer_afm_clean]
                    if found_name and str(found_name).strip():
                        log.info(f"Found issuer name in client_db for AFM {issuer_afm_clean}: {found_name}")
                        return str(found_name).strip()
                else:
                    log.info(f"AFM {issuer_afm_clean} not found in client_db names. Available AFMs: {len(client_map.get('names', {}))}")
            except Exception as e:
                log.exception(f"Failed to load client_db for issuer enrichment: {e}")
        else:
            log.info(f"No client_db found at: {client_db_path}")
    except ImportError as e:
        log.warning(f"Failed to import epsilon_bridge_multiclient_strict: {e}")
    except Exception as e:
        log.exception(f"Failed to resolve client_db path for issuer enrichment: {e}")
    
    # 2) Fallback: Ψάχνουμε με VAT validator
    try:
        from vat_validator import validate_greek_vat
        
        # Χρησιμοποιούμε το normalized AFM από το βήμα 1
        try:
            from epsilon_bridge_multiclient_strict import _norm_afm as bridge_norm_afm
            issuer_afm_clean = bridge_norm_afm(issuer_afm)
        except:
            issuer_afm_clean = str(issuer_afm).strip()
        
        log.info(f"Attempting VAT validation for issuer AFM {issuer_afm_clean}")
        result = validate_greek_vat(issuer_afm_clean)
        
        if result.get("valid") and result.get("name"):
            log.info(f"Found issuer name via VAT validator for AFM {issuer_afm_clean}: {result['name']}")
            return result["name"]
        elif result.get("error"):
            log.warning(f"VAT validator error for AFM {issuer_afm_clean}: {result['error']}")
    except ImportError:
        log.warning("vat_validator module not available for issuer enrichment")
    except Exception as e:
        log.exception(f"VAT validation failed for issuer AFM {issuer_afm}: {e}")
    
    return None


def _looks_like_receipt(rec: dict) -> bool:
    t = f"{rec.get('DOCTYPE','')} {rec.get('type','')} {rec.get('category','')} {rec.get('series','')}".lower()
    return any(k in t for k in ("receipt", "αποδειξ", "λιαν"))

def _collect_missing_afm_from_preview(rows):
    missing = {}
    for r in rows or []:
        cid = r.get("CUSTID")
        if cid not in (None, "", 0):
            continue
        afm = _norm_afm(r.get("AFM_ISSUER") or r.get("AFM") or "")
        if not afm:
            continue
        name = (r.get("ISSUER_NAME") or r.get("Name") or "").strip()
        aa = r.get("AA") or r.get("aa")
        if afm not in missing:
            missing[afm] = {"afm": afm, "name": name, "count": 0, "examples": []}
        missing[afm]["count"] += 1
        if aa and len(missing[afm]["examples"]) < 5:
            missing[afm]["examples"].append(str(aa))
    return missing

def _make_temp_client_db_with_new_ids(
    vat: str,
    base_client_db_path: str,
    missing_map: dict,
    invoices_fallback_json: str | None = None
) -> str:
    import os as _os, math as _math, tempfile as _tmp
    import pandas as _pd

    # --- Διάβασε ΟΠΩΣ ΕΙΝΑΙ το υπάρχον αρχείο (xls/xlsx) κρατώντας φύλλο & στήλες ---
    if not _os.path.exists(base_client_db_path):
        df = _pd.DataFrame(columns=["Κωδ. Συναλλασσόμενου","ΑΦΜ","Επωνυμία"])
        sheet_name = "ΣΥΝΑΛΛΑΣΣΟΜΕΝΟΙ"
    else:
        x = _pd.ExcelFile(base_client_db_path)  # διαβάζει .xls/.xlsx
        # πάρε πρώτο φύλλο όπως είναι — ή ένα από τα γνωστά
        preferred = ["ΣΥΝΑΛΛΑΣΣΟΜΕΝΟΙ","Συναλλασσόμενοι","Clients","clients","Συναλλ/νοι"]
        sheet_name = next((s for s in preferred if s in x.sheet_names), x.sheet_names[0])
        df = _pd.read_excel(base_client_db_path, sheet_name=sheet_name, dtype=str).fillna("")

    # βρες ΠΡΑΓΜΑΤΙΚΑ ονόματα για code/afm/name με βάση τους ελληνικούς τίτλους σου
    def _find_col(df, aliases):
        low = {c: str(c).strip().lower() for c in df.columns}
        for c, lc in low.items():
            for a in aliases:
                if a in lc:
                    return c
        return None

    code_col = _find_col(df, ["κωδ. συναλλασσόμενου", "κωδ", "custid", "code"]) or "Κωδ. Συναλλασσόμενου"
    afm_col  = _find_col(df, ["αφμ", "afm"]) or "ΑΦΜ"
    name_col = _find_col(df, ["επωνυμία", "επων", "name", "ονομα"]) or "Επωνυμία"

    # εξασφάλισε ότι υπάρχουν οι τρεις βασικές στήλες
    for col in (code_col, afm_col, name_col):
        if col not in df.columns:
            df[col] = ""

    # υπάρχοντα AFM & χρησιμοποιημένοι κωδικοί
    def _norm9(s: str) -> str:
        s = "".join(ch for ch in str(s) if ch.isdigit())
        return s.zfill(9) if s else ""

    existing_afm = set(_norm9(v) for v in df[afm_col].astype(str).tolist() if str(v).strip())

    used_ids = set()
    for v in df[code_col].astype(str).tolist():
        try:
            fv = float(str(v).replace(",", "."))
            if not _math.isnan(fv) and fv.is_integer():
                used_ids.add(int(fv))
        except Exception:
            continue
    next_id = (max(used_ids) + 1) if used_ids else 1

    # fallback ονόματα από τα raw invoices
    afm_to_name = {}
    if invoices_fallback_json and _os.path.exists(invoices_fallback_json):
        try:
            invs = _safe_json_read(invoices_fallback_json, default=[])
            if isinstance(invs, list):
                for r in invs:
                    iafm = _norm_afm(r.get("AFM_issuer") or r.get("AFM") or "")
                    nm = (r.get("Name_issuer") or r.get("issuerName") or r.get("issuer_name") or "").strip()
                    if iafm and nm and iafm not in afm_to_name:
                        afm_to_name[iafm] = nm
        except Exception:
            pass

    # μόνο οι όντως νέοι AFM
    new_rows = []
    for afm, info in (missing_map or {}).items():
        afm_norm = _norm9(afm)
        if not afm_norm or afm_norm in existing_afm:
            continue
        nm = (info or {}).get("name") or afm_to_name.get(afm_norm) or ""
        # Γράψε ΓΡΑΜΜΗ με ΟΛΕΣ τις ΥΠΑΡΧΟΥΣΕΣ στήλες του αρχείου χωρίς να αλλάξεις layout
        row = {col: "" for col in df.columns}
        row[code_col] = str(next_id)
        row[afm_col]  = afm_norm
        row[name_col] = nm
        new_rows.append(row)
        existing_afm.add(afm_norm)
        next_id += 1

    if not new_rows:
        return base_client_db_path  # τίποτα να προσθέσουμε

    df_out = _pd.concat([df, _pd.DataFrame(new_rows, columns=df.columns)], ignore_index=True)

    # φόρμαρε τα AFM σαν 9-ψήφια string
    df_out[afm_col] = df_out[afm_col].astype(str).str.replace(r"\D+", "", regex=True).str.zfill(9)

    # γράψε προσωρινό αρχείο ΚΡΑΤΩΝΤΑΣ ΤΟ ΙΔΙΟ sheet_name
    fd, tmp_path = _tmp.mkstemp(prefix=f"{vat}_client_db_tmp_", suffix=".xlsx")
    _os.close(fd)
    with _pd.ExcelWriter(tmp_path, engine="xlsxwriter") as w:
        df_out.to_excel(w, index=False, sheet_name=sheet_name)
    return tmp_path

def _load_client_map_smart(path: str) -> dict:
    """
    Φορτώνει το client_db και επιστρέφει:
      {
        "by_afm": { "<AFM_DIGITS>": <CUSTID>, ... },
        "by_id":  { <CUSTID>, ... },
        "cols":   [list of original columns]
      }
    - Διαβάζει ΠΑΝΤΑ το 1ο sheet (ανεξαρτήτως ονόματος).
    - Κανονικοποιεί AFM (μόνο ψηφία) και CUSTID (int όπου γίνεται, αλλιώς string).
    - Σε διπλότυπα AFM, κρατάει την τελευταία μη-κενή τιμή CUSTID.
    """
    result = {"by_afm": {}, "by_id": set(), "cols": []}
    if not path or not os.path.exists(path):
        return result

    df = _read_first_sheet_anyname(path)
    if df.empty:
        return result

    afm_col, id_col = _detect_cols(df)
    result["cols"] = list(df.columns)

    if not afm_col or not id_col:
        current_app.logger.warning(
            "client_db: could not detect AFM/ID columns. cols=%r", result["cols"]
        )
        return result

    by_afm: dict[str, int | str] = {}
    by_id: set[int | str] = set()

    for _, r in df.iterrows():
        afm_raw = r.get(afm_col, None)
        id_raw = r.get(id_col, None)
        afm = _canon_afm(afm_raw)
        custid = _coerce_id(id_raw)
        if afm and custid is not None:
            by_afm[afm] = custid
            by_id.add(custid)

    result["by_afm"] = by_afm
    result["by_id"] = by_id

    current_app.logger.info(
        "client_db: mapped=%d (AFM col=%r, ID col=%r)", len(by_afm), afm_col, id_col
    )
    return result

def _guess_partner_afm(rec: dict, active_vat: str) -> str:
    """
    Βρίσκει το AFM του συναλλασσόμενου για mapping σε CUSTID:
      - Αν AFM_issuer == active_vat  ⇒ πώληση ⇒ πελάτης (counterparty AFM)
      - Αλλιώς                        ⇒ αγορά   ⇒ προμηθευτής (AFM_issuer)
    Επιστρέφει AFM κανονικοποιημένο (μόνο ψηφία όπου γίνεται).
    """
    issuer = str(rec.get("AFM_issuer") or rec.get("AFM") or "").strip()
    issuer = _canon_afm(issuer)
    active_vat = _canon_afm(active_vat)

    if issuer and issuer == active_vat:
        # Πελάτης (counterparty)
        for k in (
            "AFM_counterparty", "AFM_customer", "customerAFM", "buyerAFM",
            "AFM_buyer", "counterparty_afm", "customer_afm", "AFM_other"
        ):
            v = rec.get(k)
            if v:
                return _canon_afm(v)
        return ""  # λείπει στο JSON => δεν μπορούμε να χαρτογραφήσουμε
    else:
        # Προμηθευτής (issuer)
        return issuer


def _load_client_map(path: str):
    # require openpyxl for .xlsx, xlrd για .xls
    df = pd.read_excel(path)
    # heuristic στήλες
    col_afm = col_id = None
    for c in df.columns:
        lc = str(c).lower()
        if col_afm is None and ("αφμ" in lc or lc == "afm" or "vat" in lc): col_afm = c
        if col_id is None and ("συναλλασ" in lc or "κωδ" in lc or lc == "id" or "custid" in lc): col_id = c
    if col_afm is None:
        for c in df.columns:
            if "αφ" in str(c).lower(): col_afm = c; break
    if col_id is None:
        for c in df.columns:
            if "id" in str(c).lower() or "κωδ" in str(c).lower(): col_id = c; break
    by_afm = {}
    for _, r in df.iterrows():
        afm = str(r.get(col_afm, "")).strip()
        try:
            custid = int(float(r.get(col_id)))
        except Exception:
            custid = None
        if afm and custid is not None:
            by_afm[afm] = custid
    return by_afm

def _ddmmyyyy(s):
    if not s: return ""
    for fmt in ("%Y-%m-%d","%d/%m/%Y","%d-%m-%Y","%Y/%m/%d","%d/%m/%y"):
        try: return datetime.strptime(str(s)[:10], fmt).strftime("%d/%m/%Y")
        except: pass
    return str(s)

def _load_epsilon_invoices(vat: str):
    vat = str(vat)
    cands = [
        f"data/epsilon/{vat}_epsilon_invoices.json",
        "data/epsilon/epsilon_invoices.json",
        f"{vat}_epsilon_invoices.json",
    ]
    path = next((p for p in cands if os.path.exists(p)), None)
    if not path:
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        for k in ("invoices","records","rows","data","items"):
            if isinstance(data.get(k), list):
                return data[k]
        return [data]
    return data

def _sum_lines(rec):
    lines = rec.get("lines") or rec.get("invoice_lines") or rec.get("details") or []
    if not isinstance(lines, list) or not lines:
        # fallback από totals
        net = float(str(rec.get("totalNetValue","0")).replace(",", ".") or 0)
        vat = float(str(rec.get("totalVatAmount","0")).replace(",", ".") or 0)
        return net, vat, net+vat, []
    detail = []
    tot_net = 0.0; tot_vat = 0.0
    for ln in lines:
        net = float(str(ln.get("amount","0")).replace(",", ".") or 0)
        vat = float(str(ln.get("vat","0")).replace(",", ".") or 0)
        vat_label = ln.get("vat_category") or ln.get("vatRate")
        m = re.search(r"(\d+)", str(vat_label) or "")
        vat_rate = int(m.group(1)) if m else None
        cat = (ln.get("category") or "").strip()
        detail.append({
            "category": cat,
            "net": net,
            "vat": vat,
            "gross": net+vat,
            "vat_rate": vat_rate
        })
        tot_net += net; tot_vat += vat
    return tot_net, tot_vat, tot_net+tot_vat, detail
def _ensure_paths():
    global CREDENTIALS_PATH
    if isinstance(CREDENTIALS_PATH, str):
        CREDENTIALS_PATH = Path(CREDENTIALS_PATH)
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)

def _load_json_file(path: Path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return default if data is None else data
    except Exception:
        return default

def _save_json_file(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def _load_credentials():
    _ensure_paths()
    return _load_json_file(CREDENTIALS_PATH, [])

def _save_credentials(creds):
    _ensure_paths()
    _save_json_file(CREDENTIALS_PATH, creds)

# ---- credentials helpers (δουλεύουν και με list και με dict-style) ----
def _get_customer(creds, vat: str, create=False):
    vat = (vat or "").strip()
    if isinstance(creds, list):
        for i, c in enumerate(creds):
            if str(c.get("vat", "")).strip() == vat:
                return c, ("list", i)
        if create and vat:
            newc = {"vat": vat, "expense_tags": [], "custom_categories": []}
            creds.append(newc)
            return newc, ("list", len(creds)-1)
        return None, ("list", None)
    elif isinstance(creds, dict):
        customers = creds.setdefault("customers", {})
        if vat in customers:
            return customers[vat], ("dict", vat)
        if create and vat:
            customers[vat] = {"vat": vat, "expense_tags": [], "custom_categories": []}
            return customers[vat], ("dict", vat)
        return None, ("dict", None)
    return None, ("unknown", None)

def _get_expense_tags(creds, vat: str):
    cust, _ = _get_customer(creds, vat, create=False)
    return _list_invoice_categories(cust)

def _get_char_profiles(creds, vat: str):
    cust, _ = _get_customer(creds, vat, create=False)
    if isinstance(cust, dict):
        lst = cust.get("char_profiles") or []
        # sanitize
        out = []
        for p in lst:
            mp = p.get("mapping") or {}
            # κρατάμε μόνο τα ποσοστά
            mapping = {k: v for k, v in mp.items() if k in VAT_KEYS and (v or "").strip()}
            out.append({"id": p.get("id") or p.get("name") or str(uuid.uuid4()),
                        "name": p.get("name") or "",
                        "mapping": mapping,
                        "updated_at": p.get("updated_at")})
        return out
    return []

def _set_char_profiles(creds, vat: str, profiles: list):
    cust, where = _get_customer(creds, vat, create=True)
    if not isinstance(cust, dict):
        return creds
    cust["char_profiles"] = profiles
    # για list δεν χρειάζεται ειδικό χειρισμό, το dict είναι reference
    return creds


def _normalize_char_profile(entry: dict) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    mapping = entry.get("mapping") if isinstance(entry.get("mapping"), dict) else None
    if mapping is None and isinstance(entry.get("map"), dict):
        mapping = entry.get("map")
    profile = dict(entry)
    profile["mapping"] = mapping or {}
    profile["mode"] = str(entry.get("mode") or ("" if entry.get("mode") == "" else "invoices")).strip().lower()
    profile["id"] = str(entry.get("id") or "").strip()
    profile["invoice_mtype"] = str(entry.get("invoice_mtype") or "").strip()
    profile["receipt_mtype"] = str(entry.get("receipt_mtype") or "").strip()
    return profile


def _filter_char_profiles_by_mode(raw_profiles: list, mode: str) -> List[dict]:
    normalized: List[dict] = []
    if isinstance(raw_profiles, list):
        for entry in raw_profiles:
            prof = _normalize_char_profile(entry)
            if prof:
                normalized.append(prof)

    target_mode = (mode or "invoices").strip().lower()
    if target_mode == "receipts":
        return [p for p in normalized if (p.get("mode") in ("receipts", ""))]
    return [p for p in normalized if (p.get("mode") or "invoices") == "invoices"]


def _is_general_profile(profile: dict) -> bool:
    name = str((profile or {}).get("name") or "").strip()
    if not name:
        return True
    normalized = name.casefold()
    return normalized in {"γενικο", "γενικό", "general", "default"}

def _get_repeat_entry(creds, vat: str):
    cust, _ = _get_customer(creds, vat, create=False)
    if not isinstance(cust, dict):
        return {"enabled": False, "mapping": {}}
    rep = cust.get("repeat_entry") or {}
    payload = _build_repeat_entry_payload(rep)
    payload["enabled"] = bool(rep.get("enabled"))
    return payload

def _save_repeat_entry(creds, vat: str, enabled: bool, mapping: dict):
    cust, _ = _get_customer(creds, vat, create=True)
    mapping = {k: (mapping.get(k) or "").strip() for k in VAT_KEYS}
    cust["repeat_entry"] = {
        "enabled": bool(enabled),
        "mapping": mapping,
        "general_mapping": dict(mapping),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
    }
    return creds

# --- helper: parse year from a variety of date strings ---
APP_DIR = Path(__file__).resolve().parent
SCRAPER_PATH = APP_DIR / "scraper_receipt.py"

# --- add near the top of app.py (μαζί με τα υπόλοιπα imports)


# --- add near your other small helpers ---
def _meaningful_summary(d: dict) -> bool:
    """True αν υπάρχει 15ψήφιο MARK ή τουλάχιστον μία ουσιαστική γραμμή/σύνολο."""
    try:
        if not isinstance(d, dict):
            return False

        mark = str(d.get("mark") or d.get("MARK") or "").strip()
        if re.fullmatch(r"\d{15}", mark):
            return True

        # Γραμμές με ουσία (id ή περιγραφή ή ποσό)
        lines = d.get("lines") or []
        for ln in lines:
            if not ln:
                continue
            if str(ln.get("id") or ln.get("line_id") or "").strip():
                return True
            if str(ln.get("description") or ln.get("desc") or "").strip():
                return True
            if str(ln.get("amount") or ln.get("lineTotal") or ln.get("total") or "").strip():
                return True

        # Ή συνολικά ποσά (αν υπάρχουν)
        total = str(d.get("totalValue") or d.get("total_amount") or "").strip()
        if total and total not in ("0", "0.00", "0,00"):
            return True

        return False
    except Exception:
        return False

@monitor_resources('save_receipt')
def save_receipt(afm: str, year: str, summary: dict, lines: list, active_years: list):
    """
    Αποθηκεύει μια απόδειξη στα αρχεία AFM_invoices.xlsx και AFM_epsilon_invoices.json
    με τον ίδιο τρόπο που αποθηκεύονται τα τιμολόγια.
    """

    # Έλεγχος ενεργής χρήσης
    issue_date = datetime.strptime(summary["issueDate"], "%d/%m/%Y")
    if int(year) not in active_years or issue_date.year != int(year):
        return {"ok": False, "error": "Απόδειξη εκτός ενεργής χρήσης"}

    # Recompute totals for analysis receipts (UI may pass raw values)
    try:
        if summary.get("receipt_analysis_enabled") and isinstance(lines, list):
            tot_net = 0.0
            tot_vat = 0.0
            for ln in lines:
                try:
                    tot_net += float_from_comma(ln.get("amount") or 0)
                except Exception:
                    pass
                try:
                    tot_vat += float_from_comma(ln.get("vat") or 0)
                except Exception:
                    pass
            summary["totalNetValue"] = f"{tot_net:.2f}"
            summary["totalVatAmount"] = f"{tot_vat:.2f}"
            summary["totalValue"] = f"{(tot_net + tot_vat):.2f}"
    except Exception:
        log.exception("save_receipt: failed to recalc totals")

    # Προετοιμασία φακέλων/αρχειων
    invoices_file = f"data/{afm}_invoices.xlsx"
    epsilon_file = f"data/epsilon/{afm}_epsilon_invoices.json"
    os.makedirs(os.path.dirname(invoices_file), exist_ok=True)
    os.makedirs(os.path.dirname(epsilon_file), exist_ok=True)

    # Προσθήκη τύπου "αποδειξακια"
    summary["type"] = "αποδειξακια"
    for line in lines:
        line["type"] = "αποδειξακια"

    # --- Ενημέρωση JSON ---
    epsilon_data = []
    if os.path.exists(epsilon_file):
        with open(epsilon_file, "r", encoding="utf-8") as f:
            try:
                epsilon_data = json.load(f)
            except:
                epsilon_data = []

    epsilon_data.append({"summary": summary, "lines": lines})

    with open(epsilon_file, "w", encoding="utf-8") as f:
        json.dump(epsilon_data, f, ensure_ascii=False, indent=2)

    # --- Ενημέρωση Excel ---
    df_summary = pd.DataFrame([summary])
    df_lines = pd.DataFrame(lines)

    if os.path.exists(invoices_file):
        with pd.ExcelWriter(invoices_file, mode="a", if_sheet_exists="overlay", engine="openpyxl") as writer:
            df_summary.to_excel(writer, sheet_name="summary", index=False, header=False, startrow=writer.sheets["summary"].max_row)
            df_lines.to_excel(writer, sheet_name="lines", index=False, header=False, startrow=writer.sheets["lines"].max_row)
    else:
        with pd.ExcelWriter(invoices_file, engine="openpyxl") as writer:
            df_summary.to_excel(writer, sheet_name="summary", index=False)
            df_lines.to_excel(writer, sheet_name="lines", index=False)

    return {"ok": True}

@monitor_resources('run_scraper_subprocess')
def run_scraper_subprocess(arg=None, timeout=240):
    """
    Καλεί scraper_receipt.py με τον ίδιο python interpreter (sys.executable).
    Επιστρέφει dict: { ok: bool, data: dict|None, error: str|None, stdout, stderr }
    """
    python_exec = sys.executable or "python3"
    cmd = [python_exec, str(SCRAPER_PATH)]
    if arg is not None:
        cmd.append(str(arg))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return {"ok": False, "data": None, "error": f"subprocess_failed:{e}", "stdout": "", "stderr": ""}

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"ok": False, "data": None, "error": f"scraper_exit_{proc.returncode}", "stdout": stdout, "stderr": stderr}

    # proc.returncode == 0 -> try parse stdout as JSON
    if not stdout:
        return {"ok": False, "data": None, "error": "empty_stdout_from_scraper", "stdout": "", "stderr": stderr}
    try:
        parsed = json.loads(stdout)
        return {"ok": True, "data": parsed, "error": None, "stdout": stdout, "stderr": stderr}
    except Exception:
        # scraper completed but didn't emit JSON — return raw stdout for debugging
        return {"ok": False, "data": None, "error": "invalid_json_from_scraper", "stdout": stdout, "stderr": stderr}

def get_excel_path(afm, year):
    return os.path.join(BASE_DIR, f"{afm}_{year}_invoices.xlsx")

def get_json_path(afm):
    return os.path.join(BASE_DIR, f"{afm}_epsilon_invoices.json")

def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_excel(path):
    if os.path.exists(path):
        return load_workbook(path)
    else:
        wb = Workbook()
        ws = wb.active
        ws.append(["MARK", "Issue Date", "Total Amount", "Type"])  # header
        return wb

def save_excel(wb, path):
    wb.save(path)

def mark_matches_year(receipt, year):
    """Check if issue_date matches selected year"""
    try:
        issue_year = dt.strptime(receipt["issue_date"], "%d/%m/%Y").year
        return issue_year == int(year)
    except:
        return False
def parse_year_from_date_string(s):
    if not s:
        return None
    s = str(s).strip()
    # try common date formats
    patterns = [
        '%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y', '%d-%m-%Y',
        '%d.%m.%Y', '%Y.%m.%d'
    ]
    for p in patterns:
        try:
            dt = datetime.strptime(s, p)
            return dt.year
        except Exception:
            pass
    # fallback: find any 4-digit year
    m = re.search(r'(19|20)\d{2}', s)
    if m:
        return int(m.group(0))
    return None

# --- helper: persistent 15-digit MARK generator ---
def get_next_mark():
    """
    Read/update MARK_COUNTER_PATH to return next integer as 15-digit zero-padded string.
    Keeps a single counter. Thread/process safe-ish by using FileLock.
    """
    lock_path = MARK_COUNTER_PATH + '.lock'
    lock = FileLock(lock_path, timeout=5)
    with lock:
        try:
            if os.path.exists(MARK_COUNTER_PATH):
                with open(MARK_COUNTER_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                data = {'last': 0}
        except Exception:
            data = {'last': 0}
        last = int(data.get('last', 0) or 0)
        nxt = last + 1
        data['last'] = nxt
        with open(MARK_COUNTER_PATH, 'w', encoding='utf-8') as f:
            json.dump(data, f)
    return str(nxt).zfill(15)

# --- helper: append receipt entry to Excel (pandas) with file lock ---
def append_receipt_to_excel(entry, excel_path=EPSILON_EXCEL_PATH):
    """
    Γράφει απόδειξη στο Excel με σίγουρο Α/Α + τύπο/είδος.
    - Coalesce AA από πολλά κλειδιά (entry/raw/summary).
    - Γράφει AA στα: 'Α/Α', 'Α/Α Παραστατικού', 'progressive_aa'
    - Γράφει τύπο/είδος στα: 'τύπος','τυπος','είδος','ειδος'
    - Όλα ως string.
    """
    import os, json
    import pandas as pd
    from filelock import FileLock

    # ---- helpers ----
    def _first(*vals):
        for v in vals:
            if v is None: 
                continue
            s = str(v).strip()
            if s and s.lower() not in ('none','nan'):
                return s
        return ''

    # Κάποια flows περνάνε "summary" μέσα στο entry· κάνε coalesce κι από εκεί
    raw   = (entry.get('raw') or {}) if isinstance(entry, dict) else {}
    summ  = (entry.get('summary') or {}) if isinstance(entry, dict) else {}
    # if receipt has analysis and lines present, compute aggregated net/vat
    try:
        if summ.get('receipt_analysis_enabled') and isinstance(summ.get('lines'), list):
            total_net = 0.0
            total_vat = 0.0
            for ln in summ.get('lines', []):
                # use shared helper that strips thousands separators and
                # converts comma to dot so "5.645,00" -> 5645.00 correctly
                amt = float_from_comma(ln.get('amount') or 0)
                vat_raw = ln.get('vat') or ''
                vat_amt = 0.0
                if isinstance(vat_raw, str) and '%' in vat_raw:
                    try:
                        pct = float_from_comma(vat_raw.replace('%', ''))
                        vat_amt = amt * pct / 100.0
                    except Exception:
                        vat_amt = 0.0
                else:
                    vat_amt = float_from_comma(vat_raw)
                total_net += amt
                total_vat += vat_amt
            summ['totalNetValue'] = f"{total_net:.2f}"
            summ['totalVatAmount'] = f"{total_vat:.2f}"
    except Exception:
        pass

    aa = _first(
        entry.get('progressive_aa'), entry.get('AA'), entry.get('aa'), entry.get('receipt_aa'),
        entry.get('Α/Α'), entry.get('Α/Α Παραστατικού'),
        raw.get('progressive_aa'), raw.get('AA'), raw.get('aa'), raw.get('receipt_aa'),
        summ.get('progressive_aa'), summ.get('AA'), summ.get('aa')
    )

    # Τύπος/Είδος: προτιμάμε ό,τι έρχεται, αλλιώς 'αποδειξη'
    tipo = _first(entry.get('τύπος'), entry.get('τυπος'), raw.get('τύπος'), raw.get('τυπος'),
                  summ.get('τύπος'), summ.get('τυπος'), 'αποδειξη')
    eidos = _first(entry.get('είδος'), entry.get('ειδος'), raw.get('είδος'), raw.get('ειδος'),
                   summ.get('είδος'), summ.get('ειδος'), 'αποδειξη')

    lock_path = excel_path + '.lock'
    lock = FileLock(lock_path, timeout=10)
    with lock:
        flat = {
            'MARK':            _first(entry.get('MARK'), entry.get('mark'), summ.get('MARK'), summ.get('mark')),
            'saved_at':        _first(entry.get('saved_at'), summ.get('saved_at')),
            'url':             _first(entry.get('url'), summ.get('url')),
            'issuer_vat':      _first(entry.get('issuer_vat'), raw.get('issuer_vat'), summ.get('issuer_vat')),
            'issuer_name':     _first(entry.get('issuer_name'), raw.get('issuer_name'), summ.get('issuer_name')),
            'issue_date':      _first(entry.get('issue_date'), raw.get('issue_date'), summ.get('issue_date')),
            # A/A σε όλα τα headers που μπορεί να κοιτά το template
            'progressive_aa':  aa,
            'Α/Α':             aa,
            'Α/Α Παραστατικού':aa,
            # Τύπος/Είδος σε όλα τα headers (με και χωρίς τόνο)
            'τύπος':           tipo,
            'τυπος':           tipo,
            'είδος':           eidos,
            'ειδος':           eidos,
            'total_amount':    _first(entry.get('total_amount'), raw.get('total_amount'), summ.get('total_amount')),
            # if receipt has analysis calculate net/vat from summary lines
            'totalNetValue':   _first(entry.get('totalNetValue'), summ.get('totalNetValue'), None),
            'totalVatAmount':  _first(entry.get('totalVatAmount'), summ.get('totalVatAmount'), None),
            # προαιρετικό: αποθήκευσε το raw για debugging
            'raw':             json.dumps(entry.get('raw', {}), ensure_ascii=False)
        }

        # όλα ως string
        for k, v in list(flat.items()):
            flat[k] = '' if v is None else str(v)

        base_cols = [
            'MARK','saved_at','url','issuer_vat','issuer_name','issue_date',
            'progressive_aa','Α/Α','Α/Α Παραστατικού',
            'τύπος','τυπος','είδος','ειδος',
            'total_amount','totalNetValue','totalVatAmount','raw'
        ]

        if os.path.exists(excel_path):
            try:
                df = pd.read_excel(excel_path, dtype=str).fillna('')
            except Exception:
                df = pd.DataFrame(columns=base_cols)
            for c in base_cols:
                if c not in df.columns:
                    df[c] = ''
            df_new = pd.DataFrame([flat], columns=base_cols).fillna('')
            df = pd.concat([df, df_new], ignore_index=True, sort=False)
        else:
            df = pd.DataFrame([flat], columns=base_cols).fillna('')

        # Επιμονή σε string στα κρίσιμα
        for col in ['progressive_aa','Α/Α','Α/Α Παραστατικού','τύπος','τυπος','είδος','ειδος']:
            if col in df.columns:
                df[col] = df[col].astype(str).fillna('')

        df.to_excel(excel_path, index=False)


def _repeat_state_path():
    # DATA_DIR υπάρχει ήδη στο app σου
    return group_path("repeat_state.json")

def _repeat_state_load():
    p = _repeat_state_path()
    try:
        if not os.path.exists(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _repeat_state_save(d: dict):
    p = _repeat_state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d or {}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)

def _repeat_state_get_enabled_for_vat(vat: str):
    # σειρά προτεραιότητας: session -> repeat_state.json -> credentials.repeat_entry.enabled -> False
    try:
        if "repeat_enabled" in session:
            return bool(session.get("repeat_enabled"))
    except Exception:
        pass
    try:
        data = _repeat_state_load()
        if vat and vat in data:
            return bool(data[vat].get("enabled", False))
    except Exception:
        pass
    try:
        cred = get_active_credential_from_session() or {}
        re = cred.get("repeat_entry") or {}
        return bool(re.get("enabled", False))
    except Exception:
        pass
    return False

def _sync_repeat_entry_backend(vat: str, *, enabled=None, invoice_mtype=None, receipt_mtype=None):
    """Best-effort sync of repeat_entry fields to credentials + session + repeat_state.json."""
    v = str(vat or "").strip()
    if not v:
        return False

    changed = False
    try:
        creds = read_credentials_list() or []
        idx = find_active_client_index(creds, vat=v) if 'find_active_client_index' in globals() else None
        if idx is None:
            for i, c in enumerate(creds):
                if not isinstance(c, dict):
                    continue
                cand = str(c.get("vat") or c.get("AFM") or c.get("tax_number") or "").strip()
                if cand == v:
                    idx = i
                    break

        if idx is not None and 0 <= idx < len(creds) and isinstance(creds[idx], dict):
            client = creds[idx]
            repeat = (client.get("repeat_entry") if isinstance(client, dict) else {}) or {}

            if enabled is not None and bool(repeat.get("enabled")) != bool(enabled):
                repeat["enabled"] = bool(enabled)
                changed = True
            if invoice_mtype is not None:
                inv = str(invoice_mtype or "").strip()
                if str(repeat.get("invoice_mtype") or "").strip() != inv:
                    repeat["invoice_mtype"] = inv
                    changed = True
            if receipt_mtype is not None:
                rec = str(receipt_mtype or "").strip()
                if str(repeat.get("receipt_mtype") or "").strip() != rec:
                    repeat["receipt_mtype"] = rec
                    changed = True

            if changed:
                repeat["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                client["repeat_entry"] = repeat
                creds[idx] = client
                write_credentials_list(creds)
    except Exception:
        log.exception("_sync_repeat_entry_backend: failed credentials sync for VAT=%s", v)

    if enabled is not None:
        try:
            session["repeat_enabled"] = bool(enabled)
        except Exception:
            pass
        try:
            data = _repeat_state_load()
            if v not in data:
                data[v] = {}
            data[v]["enabled"] = bool(enabled)
            _repeat_state_save(data)
        except Exception:
            log.exception("_sync_repeat_entry_backend: failed repeat_state sync for VAT=%s", v)

    return True
def get_existing_client_ids() -> set:
    """
    Return a set of existing client IDs (ΑΦΜ) from current client_db (if any).
    Falls back to empty set if no client_db exists.
    """
    client_ids = set()
    try:
        # If Flask-Login is present and there's a current_user, restrict to their folders.
        try:
            from flask_login import current_user
            from auth import get_user_data_folders
            if getattr(current_user, 'is_authenticated', False):
                folders = get_user_data_folders(current_user) or []
                # if user has no folders, return empty set
                for folder in folders:
                    folder_path = os.path.join(BASE_DIR, 'data', folder)
                    if not os.path.isdir(folder_path):
                        continue
                    for existing in os.listdir(folder_path):
                        if existing.startswith('client_db') and os.path.splitext(existing)[1].lower() in ALLOWED_CLIENT_EXT:
                            path = os.path.join(folder_path, existing)
                            ext = os.path.splitext(path)[1].lower()
                            if ext in ['.xls', '.xlsx']:
                                df = pd.read_excel(path, dtype=str)
                            else:
                                df = pd.read_csv(path, dtype=str)
                            df.fillna('', inplace=True)
                            for afm in df.get("ΑΦΜ", []):
                                afm_str = str(afm).strip()
                                if afm_str:
                                    client_ids.add(afm_str)
                            break
                return client_ids
        except Exception:
            # fallback to global behaviour if login not available
            pass

        # αναζήτηση τρέχοντος client_db (global or per-group base)
        for existing in os.listdir(get_group_base_dir()):
            if existing.startswith('client_db') and os.path.splitext(existing)[1].lower() in ALLOWED_CLIENT_EXT:
                path = os.path.join(get_group_base_dir(), existing)
                ext = os.path.splitext(path)[1].lower()
                if ext in ['.xls', '.xlsx']:
                    df = pd.read_excel(path, dtype=str)
                else:
                    df = pd.read_csv(path, dtype=str)
                df.fillna('', inplace=True)
                for afm in df.get("ΑΦΜ", []):
                    afm_str = str(afm).strip()
                    if afm_str:
                        client_ids.add(afm_str)
                break  # παίρνουμε μόνο το πρώτο υπάρχον client_db
    except Exception:
        try:
            log.exception("Failed to get existing client IDs from client_db")
        except Exception:
            pass
    return client_ids

def _get_mark_from_epsilon_item(item):
    # normalize possible keys that may hold the mark
    for k in ("mark", "MARK", "invoice_id", "Αριθμός Μητρώου", "id"):
        if k in item and item.get(k) not in (None, ""):
            return str(item.get(k)).strip()
    return ""

def sync_epsilon_with_excel(vat):
    """
    Sync per-vat epsilon cache with the Excel file for vat:
      - Keep only epsilon entries whose mark exists in the Excel MARK column.
      - If Excel exists but has zero rows (no MARKs), epsilon will be truncated to [].
    Returns a tuple (changed: bool, removed_count: int).
    """
    try:
        excel_path = excel_path_for(vat=vat)
        if not os.path.exists(excel_path):
            log.info("sync_epsilon_with_excel: excel not found for vat %s -> skipping sync", vat)
            return False, 0

        try:
            df = pd.read_excel(excel_path, engine="openpyxl", dtype=str).fillna("")
        except Exception as e:
            log.exception("sync_epsilon_with_excel: failed reading excel %s", excel_path)
            return False, 0

        if "MARK" in df.columns:
            marks_in_excel = set(df["MARK"].astype(str).str.strip().tolist())
        else:
            # no MARK column -> treat as empty set (remove all)
            marks_in_excel = set()

        eps_list = load_epsilon_cache_for_vat(vat) or []
        # keep only those whose normalized mark is present in marks_in_excel
        kept = []
        removed = []
        for it in eps_list:
            try:
                m = _get_mark_from_epsilon_item(it)
                if m and m in marks_in_excel:
                    kept.append(it)
                else:
                    removed.append(it)
            except Exception:
                # if something weird, keep item (safer) OR you can choose to remove; here we keep
                kept.append(it)

        if len(removed) == 0:
            log.debug("sync_epsilon_with_excel: nothing to remove for vat %s (marks kept=%d)", vat, len(kept))
            return False, 0

        # persist truncated cache
        try:
            _safe_save_epsilon_cache(vat, kept)
            log.info("sync_epsilon_with_excel: removed %d epsilon entries for vat %s (kept=%d)", len(removed), vat, len(kept))
            return True, len(removed)
        except Exception:
            log.exception("sync_epsilon_with_excel: failed saving epsilon after sync for vat %s", vat)
            return False, 0

    except Exception:
        log.exception("sync_epsilon_with_excel: unexpected error for vat %s", vat)
        return False, 0

def create_empty_excel_for_vat(vat, fiscal_year=None):
    """Create an empty excel file with standard headers for given vat + fiscal_year."""
    try:
        safe_vat = secure_filename(str(vat))
    except Exception:
        safe_vat = str(vat)

    # prefer get_active_fiscal_year if fiscal_year not passed
    if fiscal_year is None:
        getter = globals().get("get_active_fiscal_year")
        try:
            if callable(getter):
                fiscal_year = getter()
        except Exception:
            fiscal_year = None
    if fiscal_year is None:
        from datetime import datetime
        fiscal_year = datetime.now().year

    # Use excel_path_for to keep filename consistent
    excel_path = excel_path_for(vat=vat)
    if os.path.exists(excel_path):
        log.debug("create_empty_excel_for_vat: excel already exists: %s", excel_path)
        return excel_path

    cols = [
        "MARK", "ΑΦΜ", "Επωνυμία", "Σειρά", "Αριθμός",
        "Ημερομηνία", "Είδος", "ΦΠΑ_ΚΑΤΗΓΟΡΙΑ",
        "Καθαρή Αξία", "ΦΠΑ", "Σύνολο"
    ]
    import pandas as pd
    df = pd.DataFrame(columns=cols).astype(str)
    try:
        os.makedirs(os.path.dirname(excel_path), exist_ok=True)
        df.to_excel(excel_path, index=False, engine="openpyxl")
        log.info("create_empty_excel_for_vat: created empty excel %s", excel_path)
    except Exception:
        log.exception("create_empty_excel_for_vat: failed creating excel %s", excel_path)
        raise
    return excel_path






import re
def _fiscal_meta_path():
    """Return path to fiscal meta file inside DATA_DIR."""
    return group_path("fiscal_meta.json")
def epsilon_item_has_detail(item):
    """
    True αν το item φαίνεται 'πραγματικό' (περιέχει αρκετά πεδία).
    False αν είναι placeholder (π.χ. μόνο mark/AFM/AA + empty lines).
    Κανόνες:
      - issueDate OR
      - lines με πραγματικά πεδία (description/amount/vat) OR
      - totalNetValue/totalValue OR
      - AFM_issuer + (AA ή issueDate ή totalValue)
    """
    try:
        if not item or not isinstance(item, dict):
            return False
        if item.get("issueDate"):
            return True
        if item.get("totalNetValue") or item.get("totalValue"):
            return True
        lines = item.get("lines") or []
        if isinstance(lines, list):
            for l in lines:
                if l and (l.get("description") or l.get("amount") or l.get("vat")):
                    return True
        # AFM_issuer alone is not enough; require some other info
        if item.get("AFM_issuer") or item.get("AFM"):
            if item.get("aa") or item.get("AA") or item.get("issueDate") or item.get("totalValue"):
                return True
    except Exception:
        pass
    return False



# --- Ensure _safe_save_epsilon_cache exists (put this near top of app.py, after imports) ---
def _safe_save_epsilon_cache(vat_code, epsilon_list):
    """
    Compatible safe writer used across the app.
    Returns the path of the saved file on success.
    Raises on failure.
    """
    epsilon_dir = group_path("epsilon")
    os.makedirs(epsilon_dir, exist_ok=True)
    safe_vat = secure_filename(str(vat_code))
    epsilon_path = os.path.join(epsilon_dir, f"{safe_vat}_epsilon_invoices.json")

    # coerce to list
    if epsilon_list is None:
        epsilon_list = []
    if not isinstance(epsilon_list, list):
        if isinstance(epsilon_list, dict):
            epsilon_list = [epsilon_list]
        else:
            try:
                epsilon_list = list(epsilon_list)
            except Exception:
                epsilon_list = [epsilon_list]

    # quick sanity: avoid overwriting with mostly-placeholders if file exists
    incomplete = 0
    try:
        for it in epsilon_list:
            if not (it and (it.get("lines") or it.get("issueDate") or it.get("AFM_issuer") or it.get("AFM"))):
                incomplete += 1
    except Exception:
        incomplete = 0

    try:
        if len(epsilon_list) > 0 and incomplete >= max(1, int(len(epsilon_list) * 0.9)):
            if os.path.exists(epsilon_path):
                log.warning("_safe_save_epsilon_cache: skipping overwrite (looks like placeholders) %s", epsilon_path)
                return epsilon_path
    except Exception:
        pass

    # Try to use json_write if defined; otherwise do atomic tmp write here
    def _sync_legacy_epsilon_shadow(saved_list):
        """Best-effort mirror to legacy group-level epsilon_invoices.json."""
        try:
            legacy_path = epsilon_json_path()
            os.makedirs(os.path.dirname(legacy_path) or ".", exist_ok=True)
            if 'json_write' in globals() and callable(globals().get('json_write')):
                json_write(legacy_path, saved_list)
            else:
                with open(legacy_path, "w", encoding="utf-8") as lf:
                    json.dump(saved_list, lf, ensure_ascii=False, indent=2)
            try:
                log.info("_safe_save_epsilon_cache: synced legacy epsilon to %s (vat=%s)", legacy_path, vat_code)
            except Exception:
                pass
        except Exception:
            log.exception("_safe_save_epsilon_cache: failed syncing legacy epsilon_invoices.json (vat=%s)", vat_code)

    try:
        # prefer json_write helper if present
        if 'json_write' in globals() and callable(globals().get('json_write')):
            json_write(epsilon_path, epsilon_list)
            _sync_legacy_epsilon_shadow(epsilon_list)
            try:
                log.info("_safe_save_epsilon_cache: saved epsilon to %s", epsilon_path)
            except Exception:
                pass
            return epsilon_path
    except Exception:
        log.exception("_safe_save_epsilon_cache: json_write failed, falling back to direct atomic write")

    # fallback atomic write
    tmp = None
    try:
        text = json.dumps(epsilon_list, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=".tmp_epsilon_", dir=epsilon_dir)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        os.replace(tmp, epsilon_path)
        _sync_legacy_epsilon_shadow(epsilon_list)
        try:
            log.info("_safe_save_epsilon_cache: saved epsilon (fallback) to %s", epsilon_path)
        except Exception:
            pass
        return epsilon_path
    except Exception:
        try:
            log.exception("_safe_save_epsilon_cache: fallback write failed")
        except Exception:
            log.exception("fallback write failed for %s", epsilon_path)
        # cleanup tmp
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise
# --- end _safe_save_epsilon_cache ---




def get_active_fiscal_year():
    """
    Read persisted fiscal year (int) from DATA_DIR/fiscal_meta.json.
    Returns integer fiscal year or None if not found / on error.
    """
    try:
        p = _fiscal_meta_path()
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not data:
            return None
        fy = data.get("fiscal_year")
        if fy is None:
            return None
        try:
            return int(fy)
        except Exception:
            # stored value not an int
            return None
    except Exception:
        try:
            log.exception("get_active_fiscal_year: failed to read fiscal meta")
        except Exception:
            pass
        return None


def set_active_fiscal_year(year):
    """Persist fiscal year (int). Returns True on success, False on failure."""
    try:
        p = _fiscal_meta_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)

        # Preserve existing metadata (for example last_fetches) and only update fiscal_year.
        data = {}
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh) or {}
            except Exception:
                data = {}

        if not isinstance(data, dict):
            data = {}
        data["fiscal_year"] = int(year)

        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        try:
            log.info("Set active fiscal year: %s", year)
        except Exception:
            pass
        return True
    except Exception:
        try:
            log.exception("Failed to write fiscal meta")
        except Exception:
            pass
        return False


def get_last_fetch_date(credential_name: str, only_meta: bool = False) -> Optional[str]:
    """
    Get the last fetch date for a credential (stored in fiscal_meta.json).
    If `only_meta` is True, only consults `fiscal_meta.json` and DOES NOT
    fall back to scanning `activity.log`.
    Returns ISO 8601 date string or None if not found.
    """
    # First try: fiscal_meta.json (existing behavior)
    try:
        p = _fiscal_meta_path()
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh) or {}
            except Exception:
                data = {}
            fetches = data.get("last_fetches", {}) if isinstance(data, dict) else {}
            if isinstance(fetches, dict) and credential_name in fetches:
                return fetches.get(credential_name)
            # If caller requested only_meta, do not fallback to activity.log
            if only_meta:
                return None
    except Exception:
        pass

    # Fallback: read activity.log and extract latest fetch timestamp for this credential (VAT)
    try:
        import re
        from datetime import datetime, timezone
        grp_base = get_group_base_dir()
        act_path = os.path.join(grp_base, 'activity.log')
        if not os.path.exists(act_path):
            return None

        candidate = None
        fetch_actions = {"fetch_data", "ληψη παραστατικων", "ληψη παραστατικων", "ληψη παραστατικών", "Λήψη Παραστατικών", "λήψη παραστατικων", "ληψη παραστατικών"}

        with open(act_path, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # try JSON line
                try:
                    obj = json.loads(line)
                    act = obj.get('action') or ''
                    if act and act in fetch_actions:
                        ts = obj.get('timestamp') or obj.get('ts') or (obj.get('details') or {}).get('timestamp')
                        if not ts:
                            continue
                        # if credential looks like VAT, filter by details.vat / πελατης
                        if re.fullmatch(r"\d{8,9}", str(credential_name)):
                            details = obj.get('details') or {}
                            v = details.get('vat') or details.get('πελατης') or details.get('client')
                            if v and str(v) != str(credential_name):
                                continue
                        try:
                            dt = datetime.fromisoformat(ts)
                        except Exception:
                            try:
                                dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f%z")
                            except Exception:
                                dt = None
                        if dt:
                            if (candidate is None) or (dt > candidate):
                                candidate = dt
                    continue
                except Exception:
                    pass

                # legacy text lines
                m = re.search(r'(?P<ts>\d{4}-\d{2}-\d{2}T[0-9:\.\+\-]+)\s+-\s+Bulk fetch performed:.*VAT\s+(?P<vat>\d+)', line)
                if m:
                    ts = m.group('ts')
                    vat = m.group('vat')
                    if re.fullmatch(r"\d{8,9}", str(credential_name)) and str(vat) != str(credential_name):
                        continue
                    try:
                        dt = datetime.fromisoformat(ts)
                    except Exception:
                        try:
                            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f%z")
                        except Exception:
                            dt = None
                    if dt:
                        if (candidate is None) or (dt > candidate):
                            candidate = dt

        if candidate is None:
            return None

        # convert to Europe/Athens and format dd/mm/YYYY HH:MM
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo('Europe/Athens')
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=timezone.utc)
            local_dt = candidate.astimezone(tz)
            return local_dt.strftime('%d/%m/%Y %H:%M')
        except Exception:
            # best-effort fallback: return ISO
            try:
                return candidate.isoformat()
            except Exception:
                return None
    except Exception:
        return None


def _format_last_fetch_date_for_display(last_date: Optional[str]) -> Optional[str]:
    """Return last-fetch timestamp formatted for the UI in Europe/Athens time."""
    if not last_date:
        return None

    try:
        try:
            dt = datetime.datetime.fromisoformat(last_date)
        except Exception:
            dt = None

        if dt is None:
            return last_date

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)

        try:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo('Europe/Athens'))
        except Exception:
            try:
                dt = dt.astimezone(datetime.timezone(datetime.timedelta(hours=2)))
            except Exception:
                pass

        return dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return last_date


def _get_fetch_tracking_key(credential_name: str = '', credential_vat: str = '') -> str:
    """Return stable key for last-fetch tracking (prefer VAT when available)."""
    vat = str(credential_vat or '').strip()
    if vat:
        return vat

    name = str(credential_name or '').strip()
    if not name:
        return ''

    try:
        creds = load_credentials() or []
        found = next((c for c in creds if str(c.get('name', '')).strip() == name), None)
        if found:
            found_vat = str(found.get('vat', '')).strip()
            if found_vat:
                return found_vat
    except Exception:
        pass

    return name


def set_last_fetch_date(credential_name: str, date_str: Optional[str] = None) -> bool:
    """
    Set the last fetch date for a credential in fiscal_meta.json.
    If date_str is None, uses current UTC time in ISO 8601 format.
    Returns True on success, False on failure.
    """
    try:
        if date_str is None:
            date_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
    
        p = _fiscal_meta_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        
        # Read existing data
        data = {}
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh) or {}
            except Exception:
                data = {}
        
        # Update last_fetches dict
        if "last_fetches" not in data:
            data["last_fetches"] = {}
        data["last_fetches"][credential_name] = date_str
        
        # Write back
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        
        try:
            log.info("Set last fetch date for credential '%s': %s", credential_name, date_str)
        except Exception:
            pass
        return True
    except Exception:
        try:
            log.exception("Failed to set last fetch date")
        except Exception:
            pass
        return False
# ---------------- normalize helper (paste/replace existing) ----------------
def _normalize_afm(raw):
    """Καθαρίζει AFM: κρατά μόνο digits, κόβει περιττά και επιστρέφει None αν άδειο."""
    if not raw:
        return None
    s = str(raw).strip()
    # extract digits
    digits = re.sub(r'\D', '', s)
    if not digits:
        return None
    # common AFM length = 9, but αν έχει περισσότερα κρατάμε τα *πρώτα* 9 (αν αυτό θέλεις)
    if len(digits) > 9:
        digits = digits[-9:]  # καλύτερο να πάρουμε τα **τελευταία 9** (συχνά οι σελίδες έχουν πρόσθετα prefix)
    return digits
# --- νέο helper: update or append a summary row into excel ---
def _normalize_summary_row_for_excel(summary):
    """Return dict with keys aligned to DEFAULT EXCEL columns used in your app."""
    out = {
        "MARK": str(summary.get("mark","") or ""),
        "AA": str(summary.get("AA","") or ""),
        "AFM": str(summary.get("AFM","") or ""),
        "Name": str(summary.get("Name","") or ""),
        "issueDate": str(summary.get("issueDate","") or ""),
        "totalValue": str(summary.get("totalValue","") or ""),
        "category": str(summary.get("category","") or ""),
        "note": str(summary.get("note","") or ""),
        "created_at": str(summary.get("created_at","") or ""),
    }
    # include net/vat columns for receipts with analysis
    try:
        if summary.get("receipt_analysis_enabled"):
            out["totalNetValue"] = str(summary.get("totalNetValue") or "")
            out["totalVatAmount"] = str(summary.get("totalVatAmount") or "")
    except Exception:
        pass
    return out

def _ensure_excel_and_update_or_append(summary: dict, vat: Optional[str] = None, cred_name: Optional[str] = None):
    """
    Update ή append στο Excel του ενεργού ΑΦΜ, ευθυγραμμισμένα ΜΟΝΟ με τα υπάρχοντα headers.
    Δεν δημιουργεί νέες στήλες. Γράφει AA/AFM/Name/issueDate/totalValue/category/note/created_at όταν υπάρχουν.
    """
    import pandas as pd, os
    path = excel_path_for(cred_name=cred_name, vat=vat)

    if isinstance(summary, dict):
        summary = dict(summary)
    else:
        try:
            summary = dict(summary or {})
        except Exception:
            summary = {}

    cred_for_series = _resolve_series_credential(summary, vat=vat, cred_name=cred_name)
    resolved_series = _resolved_series_for_summary(summary, vat=vat, cred=cred_for_series, cred_name=cred_name)
    summary["series"] = resolved_series

    # Φτιάξε αρχείο αν λείπει (κράτα τη δομή που ήδη χρησιμοποιείς)
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Αν το φτιάχνεις από την αρχή, βάλε τα headers που χρησιμοποιείς στα τιμολόγια σου
        cols = ["MARK","ΑΦΜ","Επωνυμία","Σειρά","Αριθμός","Ημερομηνία","Είδος","ΦΠΑ_ΚΑΤΗΓΟΡΙΑ","Καθαρή Αξία","ΦΠΑ","Σύνολο"]
        # αν χρησιμοποιείς «Α/Α» αντί «Αριθμός», βάλε ΚΑΙ αυτό
        if "Α/Α" not in cols:
            cols.append("Α/Α")
        pd.DataFrame(columns=cols).to_excel(path, index=False)

    try:
        df = pd.read_excel(path, engine="openpyxl", dtype=str).fillna("")
    except Exception:
        df = pd.DataFrame()

    # Αν για κάποιο λόγο είναι άδειο χωρίς headers, βάλε canonical σου
    if df.empty and not list(df.columns):
        df = pd.DataFrame(columns=["MARK","ΑΦΜ","Επωνυμία","Σειρά","Αριθμός","Ημερομηνία","Είδος","ΦΠΑ_ΚΑΤΗΓΟΡΙΑ","Καθαρή Αξία","ΦΠΑ","Σύνολο"])

    cols = list(df.columns)

    # Τιμές από το summary (υποθέτω έχει ήδη γίνει hydrate πιο πάνω)
    mark_val = str(summary.get("MARK") or summary.get("mark") or "").strip()
    afm_val  = str(summary.get("AFM") or summary.get("AFM_issuer") or "").strip()
    name_val = str(summary.get("Name") or summary.get("Name_issuer") or "").strip()
    series   = str(resolved_series or "").strip()
    number   = str(summary.get("number") or summary.get("AA") or summary.get("aa") or summary.get("progressive_aa") or "").strip()
    date_val = str(summary.get("issueDate") or "").strip()
    tipo     = str(summary.get("type") or "").strip()
    vat_cat  = str(summary.get("vatCategory") or "").strip()
    total_net = str(summary.get("totalNetValue") or "").strip()
    total_vat = str(summary.get("totalVatAmount") or "").strip()
    total_sum = str(summary.get("totalValue") or "").strip()

    is_receipt = (str(summary.get("category") or "").strip() == "αποδειξακια") or bool(summary.get("is_receipt"))

    # if this is a receipt with line-by-line analysis the totals were kept as
    # plain dot-decimal strings.  pandas/openpyxl will often convert those
    # into numeric cells, and when a Greek locale opens the workbook they'll
    # appear with **comma** as decimal separator and dot as thousands sep – in
    # other words the values no longer match the JSON we just stored.  The
    # bug only showed up for receipts that enable analysis, because invoices
    # and non-analysed receipts are already force-formatted to Greek style
    # earlier in the codepath.  To keep the sheet consistent and avoid
    # automatic locale conversion we manually rewrite the header totals to
    # use the same comma‑decimal format that the inline Excel logic uses.
    # (this change is intentionally narrow so it doesn't touch other flows)
    receipt_analysis = bool(summary.get("receipt_analysis_enabled"))
    if is_receipt and receipt_analysis:
        total_net = total_net.replace(".", ",")
        total_vat = total_vat.replace(".", ",")
        total_sum = total_sum.replace(".", ",")

    # Στήσε full row
    row_full = {
        "MARK": mark_val,
        "ΑΦΜ": afm_val,
        "Επωνυμία": name_val,
        "Σειρά": series,
        "Αριθμός": number,               # <-- για όσα αρχεία έχουν «Αριθμός»
        "Ημερομηνία": date_val,
        "Είδος": ("ΑΠΟΔΕΙΞΗ" if is_receipt else tipo),  # <-- Απόδειξη
        "ΦΠΑ_ΚΑΤΗΓΟΡΙΑ": vat_cat,
        "Καθαρή Αξία": total_net,
        "ΦΠΑ": total_vat,
        "Σύνολο": total_sum,
    }
    # Αν το αρχείο έχει στήλη «Α/Α», γέμισέ την επίσης
    if "Α/Α" in cols and not row_full.get("Α/Α"):
        row_full["Α/Α"] = number

    # Ευθυγράμμιση ΜΟΝΟ στα υπάρχοντα headers
    row_aligned = {c: row_full.get(c, "") for c in cols}

    if not mark_val:
        return  # χωρίς MARK δεν γράφουμε

    # Update logic:
    # - normal: by MARK
    # - receipt fallback MARK: only update same receipt identity (AA + Date + AFM), else append
    if "MARK" in df.columns:
        base_mask = df["MARK"].astype(str).fillna("").str.strip() == mark_val
    else:
        base_mask = pd.Series([False] * len(df), index=df.index)

    if is_receipt and mark_val == RECEIPT_FALLBACK_MARK:
        aa_col = "Αριθμός" if "Αριθμός" in df.columns else ("Α/Α" if "Α/Α" in df.columns else "")
        has_identity_cols = bool(aa_col and "Ημερομηνία" in df.columns and "ΑΦΜ" in df.columns)
        if has_identity_cols and number and date_val and afm_val:
            mask = (
                base_mask
                & (df[aa_col].astype(str).fillna("").str.strip() == number)
                & (df["Ημερομηνία"].astype(str).fillna("").str.strip() == date_val)
                & (df["ΑΦΜ"].astype(str).fillna("").str.strip() == afm_val)
            )
        else:
            mask = pd.Series([False] * len(df), index=df.index)
    else:
        mask = base_mask

    if mask.any():
        idx = mask[mask].index[0]
        for c in cols:
            df.at[idx, c] = row_aligned.get(c, df.at[idx, c])
    else:
        df = pd.concat([df, pd.DataFrame([row_aligned], columns=cols)], ignore_index=True, sort=False)

    # BONUS: Αν υπάρχει «Τύπος» αντί για «Είδος», γέμισέ το για αποδείξεις
    if "Τύπος" in df.columns and is_receipt:
        try:
            df.loc[df["MARK"].astype(str).str.strip() == mark_val, "Τύπος"] = "ΑΠΟΔΕΙΞΗ"
        except Exception:
            pass

    df.to_excel(path, index=False, engine="openpyxl")

def _extract_headers_from_upload(file_stream, ext):
    """
    Προσπαθεί να διαβάσει τα headers από το uploaded file_stream.
    - file_stream: κινούμενο binary stream στη θέση αρχής (file.read()-compatible).
    - ext: '.xlsx' / '.xls' / '.csv'
    Επιστρέφει (success: bool, headers: list[str] or None, error_msg: str or None)
    """
    # ensure stream at start
    try:
        file_stream.seek(0)
    except Exception:
        pass

    if ext == '.csv':
        # fallback χωρίς pandas: διαβάζουμε μόνο την πρώτη γραμμή
        try:
            text = file_stream.read().decode('utf-8-sig')  # handle BOM
            # move back in case caller wants to re-read
            file_stream.seek(0)
            reader = csv.reader(io.StringIO(text))
            first = next(reader, None)
            if first is None:
                return False, None, 'Το CSV φαίνεται άδειο.'
            headers = [h.strip() for h in first]
            return True, headers, None
        except Exception as e:
            return False, None, f'Σφάλμα κατά την ανάγνωση CSV headers: {e}'
    else:
        # προσπαθούμε με pandas για excel/xls
        try:
            # pandas θα διαβάσει μόνο τα headers (nrows=0) — γρήγορο
            file_stream.seek(0)
            df = pd.read_excel(file_stream, nrows=0, engine='openpyxl' if ext == '.xlsx' else None)
            headers = [str(h).strip() for h in df.columns.tolist()]
            file_stream.seek(0)
            return True, headers, None
        except Exception as e:
            # αν pandas δεν εγκατεστημένο ή άλλο σφάλμα
            return False, None, f'Αποτυχία ανάγνωσης Excel (pandas/openpyxl απαιτείται): {e}'
def _normalize_receipt_summary(summary: dict) -> dict:
    s = dict(summary or {})
    out = {}
    out['MARK'] = (s.get('MARK') or s.get('mark') or '').strip()
    out['AA'] = (s.get('AA') or s.get('progressive_aa') or s.get('id') or '').strip()
    # prefer issuer_vat, but normalize to AFM field (9 digits)
    raw_vat = (s.get('issuer_vat') or s.get('AFM') or s.get('vat') or s.get('issuerVat') or '')
    out['AFM'] = _normalize_afm(raw_vat) or ''
    out['issuer_vat_raw'] = (raw_vat or '').strip()
    out['Name'] = (s.get('issuer_name') or s.get('Name') or s.get('company') or '').strip()
    # date normalization: keep raw (you may reformat if you want)
    out['issueDate'] = (s.get('issue_date') or s.get('issueDate') or s.get('date') or '').strip()
    # total amount normalization: reuse your existing _clean_amount_to_comma if present
    total = s.get('total_amount') or s.get('totalValue') or s.get('total') or ''
    try:
        # try to keep same formatting used in invoices (comma decimal)
        out['totalValue'] = _clean_amount_to_comma(total) or str(total)
    except Exception:
        out['totalValue'] = str(total)
    out['type_name'] = (s.get('type_name') or s.get('doc_type') or 'Απόδειξη').strip()
    out['lines'] = s.get('lines') or []
    out['_saved_at'] = datetime.utcnow().isoformat() + 'Z'
    # keep category if any line already had it
    out['category'] = ''
    for ln in out['lines']:
        if isinstance(ln, dict) and ln.get('category'):
            out['category'] = ln.get('category')
            break
    return out


def _normalize_key_val(x):
    try:
        if x is None:
            return ""
        # convert numbers to str, strip whitespace, lower-case for robust comparison
        return str(x).strip()
    except Exception:
        return ""

def _find_afm_in_epsilon(mark: str = None, aa: str = None) -> str:
    """
    Search data/epsilon/*.json for an invoice matching mark or AA.
    Return AFM_issuer or AFM if found, else empty string.
    """
    try:
        epsilon_dir = group_path("epsilon")
        if not os.path.isdir(epsilon_dir):
            return ""
        for fname in os.listdir(epsilon_dir):
            if not fname.endswith("_epsilon_invoices.json"):
                continue
            try:
                path = os.path.join(epsilon_dir, fname)
                with open(path, "r", encoding="utf-8") as fh:
                    items = json.load(fh)
                if not isinstance(items, list):
                    continue
                for it in items:
                    try:
                        if mark and str(it.get("mark", "")).strip() and str(it.get("mark", "")).strip() == str(mark).strip():
                            return (it.get("AFM_issuer") or it.get("AFM") or "").strip()
                        if aa and str(it.get("AA", "")).strip() and str(it.get("AA", "")).strip() == str(aa).strip():
                            return (it.get("AFM_issuer") or it.get("AFM") or "").strip()
                    except Exception:
                        continue
            except Exception:
                # ignore corrupt epsilon files
                continue
    except Exception:
        try:
            log.exception("_find_afm_in_epsilon failed")
        except Exception:
            pass
    return ""



def _append_to_excel(rec_dict, vat: Optional[str] = None, cred_name: Optional[str] = None):
    """
    Append a single record as a row to the per-vat Excel file determined by excel_path_for().
    Uses AFM from rec_dict first, otherwise looks into epsilon cache (by mark / AA).
    """
    # determine vat to use for per-vat excel filename (fallbacks)
    vat_candidate = vat or rec_dict.get('issuer_vat') or rec_dict.get('issuer_vat_raw') or rec_dict.get('AFM') or ""
    safe_vat = secure_filename(str(vat_candidate)) if vat_candidate else ""
    path = excel_path_for(cred_name=cred_name, vat=safe_vat)

    # ensure directory exists
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Try to resolve AFM: prefer explicit issuer fields, else search epsilon cache by mark/AA
    afm = (
        (rec_dict.get('issuer_vat') or rec_dict.get('AFM_issuer') or rec_dict.get('AFM') or rec_dict.get('issuer_vat_raw') or "")
        .strip()
    )
    if not afm:
        # attempt to find AFM in epsilon cache using mark / AA
        mark = rec_dict.get("MARK") or rec_dict.get("mark") or rec_dict.get("mark_id") or ""
        aa = rec_dict.get("AA") or rec_dict.get("aa") or rec_dict.get("invoice_number") or ""
        found = _find_afm_in_epsilon(mark=mark, aa=aa)
        if found:
            afm = found.strip()

    # build row (add/adjust fields as your app expects)
    row = {
        'saved_at': rec_dict.get('_saved_at'),
        'MARK': rec_dict.get('MARK') or rec_dict.get('mark'),
        'AA': rec_dict.get('AA') or rec_dict.get('aa'),
        'AFM': afm,
        'Name': rec_dict.get('Name') or rec_dict.get('Name_issuer') or rec_dict.get('Name_counterparty') or "",
        'issueDate': rec_dict.get('issueDate') or rec_dict.get('issueDate_raw') or rec_dict.get('issue_date') or "",
        'totalValue': rec_dict.get('totalValue') or rec_dict.get('total_value') or rec_dict.get('total') or "",
        'category': rec_dict.get('category') or rec_dict.get('classification') or ""
    }

    headers = list(row.keys())

    # debug log
    try:
        log.info("append_to_excel -> path=%s vat_candidate=%s resolved_afm=%s mark=%s AA=%s",
                 path, safe_vat, row['AFM'], row.get('MARK'), row.get('AA'))
    except Exception:
        pass

    # Try pandas path first (preferred)
    try:
        import pandas as pd
        if os.path.exists(path):
            df_existing = pd.read_excel(path, engine='openpyxl', dtype=str)
        else:
            df_existing = pd.DataFrame(columns=headers)

        df_new = pd.DataFrame([row])
        df_concat = pd.concat([df_existing, df_new], ignore_index=True, sort=False)

        # Ensure all headers exist (order)
        for h in headers:
            if h not in df_concat.columns:
                df_concat[h] = ""

        df_concat.to_excel(path, index=False, engine='openpyxl')
        return True

    except Exception as e_pandas:
        # Fallback to openpyxl direct append/create
        try:
            from openpyxl import load_workbook, Workbook
            if not os.path.exists(path):
                wb = Workbook()
                ws = wb.active
                ws.append(headers)
                ws.append([row.get(k, '') for k in headers])
                wb.save(path)
                return True

            wb = load_workbook(path)
            ws = wb.active
            # ensure header row exists and matches headers; if not, add header if missing
            existing_headers = [cell.value for cell in next(ws.iter_rows(min_row=1, max_row=1))]
            if not existing_headers or all(h is None for h in existing_headers):
                # insert header then continue
                ws.insert_rows(1)
                for idx, h in enumerate(headers, start=1):
                    ws.cell(row=1, column=idx, value=h)
            ws.append([row.get(k, '') for k in headers])
            wb.save(path)
            return True

        except Exception as e_openpyxl:
            try:
                log.exception("append_to_excel failed (pandas err=%s, openpyxl err=%s)", e_pandas, e_openpyxl)
            except Exception:
                log.exception("append_to_excel failed (pandas err=%s, openpyxl err=%s)", e_pandas, e_openpyxl)
            return False
    """
    Append a single record as a row to EXCEL_FILE.
    Columns: saved_at, MARK, AA, AFM, Name, issueDate, totalValue, category
    Uses pandas (openpyxl engine).
    """
    row = {
        'saved_at': rec_dict.get('_saved_at'),
        'MARK': rec_dict.get('MARK'),
        'AA': rec_dict.get('AA'),
        'AFM': rec_dict.get('AFM'),
        'Name': rec_dict.get('Name'),
        'issueDate': rec_dict.get('issueDate'),
        'totalValue': rec_dict.get('totalValue'),
        'category': rec_dict.get('category') or ''
    }
    try:
        if os.path.exists(EXCEL_FILE):
            df_existing = pd.read_excel(EXCEL_FILE, engine='openpyxl')
        else:
            df_existing = pd.DataFrame(columns=list(row.keys()))
        df_new = pd.DataFrame([row])
        df_concat = pd.concat([df_existing, df_new], ignore_index=True, sort=False)
        df_concat.to_excel(EXCEL_FILE, index=False, engine='openpyxl')
        return True
    except Exception as e:
        current_app.logger.exception("excel append failed: %s", e)
        return False


def set_active_fiscal_year(year):
    """Persist fiscal year (int)."""
    try:
        p = _fiscal_meta_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as fh:
            json.dump({'fiscal_year': int(year)}, fh)
        log.info("Set active fiscal year: %s", year)
        return True
    except Exception:
        log.exception("Failed to write fiscal meta")
        return False
def _client_meta_path(base_dir=None):
    """Full path to metadata JSON for client_db inside a base dir (defaults to DATA_DIR)."""
    if base_dir is None:
        base_dir = get_group_base_dir()
    return os.path.join(base_dir, 'client_db.meta.json')


def read_client_meta(base_dir=None):
    """Read metadata if exists from the given base_dir (or DATA_DIR). Return dict or None."""
    meta_path = _client_meta_path(base_dir)
    try:
        if os.path.exists(meta_path):
            with open(meta_path, 'r', encoding='utf-8') as fh:
                return json.load(fh)
    except Exception:
        log.exception("Failed reading client_db.meta.json")
    return None


def write_client_meta(filename, uploaded_at_iso, base_dir=None, extra_meta=None):
    """Write metadata for client_db inside base_dir (or DATA_DIR)."""
    meta = {
        'filename': filename,
        'uploaded_at': uploaded_at_iso
    }
    if isinstance(extra_meta, dict) and extra_meta:
        meta.update(extra_meta)
    meta_path = _client_meta_path(base_dir)
    try:
        os.makedirs(os.path.dirname(meta_path), exist_ok=True)
        log.info("[Client Meta] Writing to: %s", meta_path)
        with open(meta_path, 'w', encoding='utf-8') as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        log.info("[Client Meta] Successfully wrote: %s", meta_path)
    except Exception as e:
        log.exception("[Client Meta] Failed writing client_db.meta.json to %s: %s", meta_path, e)
        raise

def normalize_vat_key(raw):
    """
    Normalize different vat-category representations into a canonical form like '24%','0%','6%','13%'
    Examples:
      'ΦΠΑ 24%' -> '24%'
      '24%' -> '24%'
      '24' -> '24%'
      'VAT 24%' -> '24%'
      '0%' -> '0%'
    Returns empty string for unknown/empty.
    """
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    if not s:
        return ""
    # remove common words like 'φπα' or 'vat'
    s = re.sub(r'φπα|\bvat\b', '', s, flags=re.IGNORECASE).strip()
    # find a number (integer or decimal) optionally followed by %
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*%?', s)
    if m:
        num = m.group(1).replace(',', '.')
        # if it's integer-like, keep integer
        if '.' in num:
            # keep as-is (rare), but normalize trailing .0
            try:
                f = float(num)
                if f.is_integer():
                    num = str(int(f))
                else:
                    # keep one or two decimals? keep as original trimmed
                    num = num.rstrip('0').rstrip('.') if '.' in num else num
            except:
                num = num
        # canonical form: without decimals if integer, with '%' suffix
        return f"{num}%"
    # if there is something else like 'μηδεν' -> map to 0%
    if re.search(r'0|μηδ', s):
        return "0%"
    return ""
CREDENTIALS_RW_LOCK = threading.RLock()


def _current_credentials_file() -> str:
    """Return credentials.json path for current request (group-aware)."""
    path = None
    try:
        if 'credentials_path_for_request' in globals():
            path = credentials_path_for_request()
    except Exception:
        path = None
    path = path or CREDENTIALS_FILE
    return path


def read_credentials_list():
    path = _current_credentials_file()
    with CREDENTIALS_RW_LOCK:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data = [data]
                return data
        except FileNotFoundError:
            return []
        except Exception:
            log.exception("read_credentials_list failed for %s", path)
            return []


def write_credentials_list(data_list):
    path = _current_credentials_file()
    os.makedirs(os.path.dirname(path) or DATA_DIR, exist_ok=True)
    tmp = path + '.tmp'
    with CREDENTIALS_RW_LOCK:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data_list, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

def find_active_client_index(creds_list, vat=None):
    # priority: match vat, else find 'active': true, else first
    if vat:
        for i,c in enumerate(creds_list):
            if str(c.get('vat','')).strip() == str(vat).strip():
                return i
    for i,c in enumerate(creds_list):
        if c.get('active'):
            return i
    return 0 if creds_list else None

def load_invoices():
    if os.path.exists(INVOICES_FILE):
        with open(INVOICES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

# φορτώνουμε epsilon_invoices.json
def load_epsilon():
    if os.path.exists(EPSILON_FILE):
        with open(EPSILON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []
def get_global_accounts_from_credentials() -> Dict:
    """
    Διαβάζει το credentials.json και επιστρέφει το αντικείμενο accounts
    αν υπάρχει ως credential με name == GLOBAL_ACCOUNTS_NAME.
    """
    creds = load_credentials()
    for c in creds:
        if c.get("name") == GLOBAL_ACCOUNTS_NAME:
            return c.get("accounts", {})
    return {}

def save_global_accounts_to_credentials(accounts: Dict) -> None:
    """
    Αποθηκεύει/ενημερώνει την εγγραφή GLOBAL_ACCOUNTS_NAME στο credentials.json
    με τα παρεχόμενα accounts mapping.
    """
    creds = load_credentials()
    found = False
    for i, c in enumerate(creds):
        if c.get("name") == GLOBAL_ACCOUNTS_NAME:
            creds[i]["accounts"] = accounts
            found = True
            break
    if not found:
        # προσθέτουμε ένα ειδικό credential αντικείμενο κρατώντας μόνο το accounts πεδίο
        creds.append({
            "name": GLOBAL_ACCOUNTS_NAME,
            "user": "",
            "key": "",
            "vat": "",
            "env": MYDATA_ENV,
            "accounts": accounts
        })
    save_credentials(creds)

# ---------------- Helpers ---------------- (most unchanged)
# -------------------------
# Robust JSON helpers
# -------------------------
def json_read(path, default=None):
    """
    Safe JSON read.
    - If file missing -> return default (default default: [] for lists, {} if you prefer)
    - If file empty/corrupt -> log and return default (do NOT raise).
    - Avoid recursive logging traps: use try/except carefully.
    """
    if default is None:
        default = []
    try:
        if not path or not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as fh:
            txt = fh.read()
            if not txt or not txt.strip():
                return default
            # load
            return json.loads(txt)
    except Exception as e:
        # try a safe fallback: don't re-call json_read here (would recurse)
        try:
            log.error("json_read failed for %s: %s", path, str(e))
        except Exception:
            # if logging itself fails, just ignore (rare)
            pass
        # return default to avoid crashing the app (caller should handle None/default)
        return default

def json_write(path, obj):
    """
    Atomic JSON write:
    - write to a tmp file, fsync, os.replace -> atomic replace
    - raises on failure so callers can handle/log
    """
    tmp = None
    try:
        dirp = os.path.dirname(path) or "."
        os.makedirs(dirp, exist_ok=True)
        text = json.dumps(obj, ensure_ascii=False, indent=2)
        # write to tmp file in same dir (for atomic replace)
        fd, tmp = tempfile.mkstemp(prefix=".tmp_json_", dir=dirp)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                # some file systems may not support fsync; ignore if fails
                pass
        # atomic replace with Windows-safe retry (handles transient file locks)
        def _replace_with_retry(src, dst, retries=5, base_delay=0.08):
            for attempt in range(retries):
                try:
                    os.replace(src, dst)
                    return
                except PermissionError as e:
                    if attempt == retries - 1:
                        raise
                    time.sleep(base_delay * (2 ** attempt))
                except OSError as e:
                    if e.errno in (errno.EBUSY, errno.EACCES, errno.ETXTBSY):
                        if attempt == retries - 1:
                            raise
                        time.sleep(base_delay * (2 ** attempt))
                    else:
                        raise

        _replace_with_retry(tmp, path)
        return True
    except Exception as e:
        try:
            log.exception("json_write failed for %s: %s", path, str(e))
        except Exception:
            pass
        # cleanup tmp if present
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


def get_expected_mtype_for_payment(payment_method_type: str) -> dict:
    """
    Επιστρέφει τον αναμενόμενο MTYPE code για ένα paymentMethodDetails.type.
    
    Mapping (βασισμένο σε οδηγίες χρήστη):
    - Types 1, 2, 6, 7, 8 → "Αγορών Εξόδων Όψεως" (MTYPE code: "3.4.1")
    - Types 4, 5 → "Αγορών Εξόδων" (MTYPE code: "3.4")
    - Type 3 → "Αγορών Εξόδων Ταμειακή" (MTYPE code: "3.4.2")
    
    Returns:
        dict με keys: "code", "label", "matched" (True/False)
    """
    pmt = str(payment_method_type).strip()
    
    # Mapping table
    PAYMENT_TO_MTYPE = {
        "1": {"code": "3.4.1", "label": "Αγορών Εξόδων Όψεως"},
        "2": {"code": "3.4.1", "label": "Αγορών Εξόδων Όψεως"},
        "6": {"code": "3.4.1", "label": "Αγορών Εξόδων Όψεως"},
        "7": {"code": "3.4.1", "label": "Αγορών Εξόδων Όψεως"},
        "8": {"code": "3.4.1", "label": "Αγορών Εξόδων Όψεως"},
        "3": {"code": "3.4.2", "label": "Αγορών Εξόδων Ταμειακή"},
        "4": {"code": "3.4", "label": "Αγορών Εξόδων"},
        "5": {"code": "3.4", "label": "Αγορών Εξόδων"},
    }
    
    if pmt in PAYMENT_TO_MTYPE:
        result = PAYMENT_TO_MTYPE[pmt].copy()
        result["matched"] = True
        return result
    else:
        # Unmapped type
        return {
            "code": "",
            "label": f"Άγνωστος τύπος πληρωμής: {pmt}",
            "matched": False
        }


def get_payment_method_label(payment_method_type: str) -> str:
    pmt = str(payment_method_type or "").strip()
    labels = {
        "1": "Επαγγελματικός Λογαριασμός Πληρωμών Ημεδαπής",
        "2": "Επαγγελματικός Λογαριασμός Πληρωμών Αλλοδαπής",
        "3": "Μετρητά",
        "4": "Επιταγή",
        "5": "Επί Πιστώσει",
        "6": "Web Banking",
        "7": "POS / e-POS",
        "8": "Άμεσες Πληρωμές IRIS",
    }
    return labels.get(pmt, "")


def load_credentials():
    # use per-group credentials loader
    try:
        return _load_all_credentials()
    except Exception:
        return []

def save_credentials(credentials):
    try:
        _save_all_credentials(credentials)
    except Exception:
        # fallback: atomic write to global credentials path
        try:
            p = Path(CREDENTIALS_FILE)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open('w', encoding='utf-8') as f:
                json.dump(credentials, f, ensure_ascii=False, indent=2)
        except Exception:
            log.exception('Failed to save credentials')

def load_settings():
    try:
        p = settings_file_path()
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        log.exception('load_settings failed')
    return {}

def save_settings(settings):
    try:
        p = settings_file_path()
        dirp = os.path.dirname(p)
        os.makedirs(dirp, exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception('save_settings failed')


def load_admin_settings() -> dict:
    """Load global admin settings from data/system/admin_settings.json (not group-scoped)."""
    try:
        p = os.path.join(ADMIN_SYSTEM_DIR, 'admin_settings.json')
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        log.exception('load_admin_settings failed')
    return {}


def save_admin_settings(settings: dict) -> None:
    """Save global admin settings to data/system/admin_settings.json (not group-scoped)."""
    try:
        os.makedirs(ADMIN_SYSTEM_DIR, exist_ok=True)
        p = os.path.join(ADMIN_SYSTEM_DIR, 'admin_settings.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        log.exception('save_admin_settings failed')

def get_active_credential():
    creds = load_credentials()
    for c in creds:
        if c.get('active'):
            return c
    return None

def add_credential(entry):
    creds = load_credentials()
    for c in creds:
        if c.get("name") == entry.get("name"):
            return False, "Credential with that name exists"
    creds.append(entry)
    save_credentials(creds)
    return True, ""

def update_credential(name, new_entry):
    creds = load_credentials()
    for i, c in enumerate(creds):
        if c.get("name") == name:
            creds[i] = new_entry
            save_credentials(creds)
            return True
    return False

def delete_credential(name):
    creds = load_credentials()
    new = [c for c in creds if c.get("name") != name]
    save_credentials(new)
    return True

def load_cache():
    try:
        data = json_read(invoices_cache_path())
        return data if isinstance(data, list) else []
    except Exception:
        return []

def save_cache(docs):
    try:
        json_write(invoices_cache_path(), docs)
    except Exception:
        log.exception('save_cache failed')

def append_doc_to_cache(doc, aade_user=None, aade_key=None):
    docs = load_cache()
    try:
        sig = json.dumps(doc, sort_keys=True, ensure_ascii=False)
    except Exception:
        sig = json.dumps(str(doc), ensure_ascii=False)
    for d in docs:
        try:
            if json.dumps(d, sort_keys=True, ensure_ascii=False) == sig:
                return False
        except Exception:
            if str(d) == str(doc):
                return False
    docs.append(doc)
    save_cache(docs)
    return True

def save_summary_list(summary_list: List[Dict]):
    """Save summary_list to SUMMARY_FILE (overwrites)."""
    try:
        json_write(summary_path(), summary_list)
    except Exception:
        log.exception("Could not write summary file")

# ---------------- small new helpers for per-customer files ----------------
def get_cred_by_name(name: str) -> Optional[Dict]:
    if not name:
        return None
    creds = load_credentials()
    return next((c for c in creds if c.get("name") == name), None)


def get_cred_by_vat(vat: Optional[str]) -> Optional[Dict[str, Any]]:
    if not vat:
        return None
    try:
        target = str(vat).strip()
        if not target:
            return None
        for cred in load_credentials():
            try:
                if str(cred.get("vat", "")).strip() == target:
                    return cred
            except Exception:
                continue
    except Exception:
        current_app.logger.exception("get_cred_by_vat failed for VAT %s", vat)
    return None


def _series_settings_from_form(form) -> Dict[str, Any]:
    try:
        mode_raw = str(form.get("series_mode") or "document").strip().lower()
    except Exception:
        mode_raw = "document"
    mode = "custom" if mode_raw == "custom" else "document"
    custom: Dict[str, str] = {}
    for key in SERIES_SETTING_KEYS:
        field = f"series_custom_{key}"
        try:
            raw_val = form.get(field)
        except Exception:
            raw_val = None
        if raw_val is None:
            continue
        val = str(raw_val).strip()
        if val:
            custom[key] = val
    if mode != "custom":
        return {"mode": "document", "custom": {}}
    return {"mode": "custom", "custom": custom}


def _series_preference_keys(summary: Optional[Dict[str, Any]]) -> Tuple[str, Optional[str]]:
    data = summary if isinstance(summary, dict) else {}
    is_receipt_flag = bool(data.get("is_receipt"))
    if not is_receipt_flag:
        try:
            tname = str(data.get("type_name") or data.get("type") or "").strip().lower()
            is_receipt_flag = ("αποδει" in tname) or ("receipt" in tname) or ("λιαν" in tname)
        except Exception:
            is_receipt_flag = False
    if is_receipt_flag:
        return "receipt_retail", None

    doc_type = str(data.get("type") or data.get("type_code") or "").strip()
    if doc_type.startswith("2."):
        return "invoice_services", SERIES_FALLBACK_MAP.get("invoice_services")
    if doc_type.startswith("5."):
        return "invoice_credit", SERIES_FALLBACK_MAP.get("invoice_credit")
    if doc_type.startswith("12."):
        return "receipt_services", SERIES_FALLBACK_MAP.get("receipt_services")
    if doc_type.startswith("13."):
        return "receipt_credit", SERIES_FALLBACK_MAP.get("receipt_credit")
    if doc_type.startswith("11."):
        return "receipt_retail", None
    if doc_type.startswith("1."):
        return "invoice_general", None
    category = str(
        data.get("category")
        or data.get("χαρακτηρισμός")
        or data.get("characteristic")
        or ""
    ).strip()
    if category == "αποδειξακια":
        return "receipt_retail", None
    return "invoice_general", None


def _resolve_series_credential(
    summary: Optional[Dict[str, Any]],
    vat: Optional[str] = None,
    cred_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cred = None
    if cred_name:
        cred = get_cred_by_name(cred_name)
    if not cred and vat:
        cred = get_cred_by_vat(vat)
    if not cred and isinstance(summary, dict):
        for key in ("AFM", "AFM_issuer", "vat"):
            candidate = summary.get(key)
            if not candidate:
                continue
            cred = get_cred_by_vat(candidate)
            if cred:
                break
    if not cred:
        try:
            cred = get_active_credential_from_session()
        except Exception:
            cred = None
    return cred


def _resolved_series_for_summary(
    summary: Optional[Dict[str, Any]],
    vat: Optional[str] = None,
    cred: Optional[Dict[str, Any]] = None,
    cred_name: Optional[str] = None,
) -> str:
    base_series = ""
    data = summary if isinstance(summary, dict) else {}
    if isinstance(summary, dict):
        base_series = str(summary.get("series") or "").strip()
    else:
        try:
            base_series = str(getattr(summary, "series", "") or "").strip()
        except Exception:
            base_series = ""
    cred_obj = cred if isinstance(cred, dict) else None
    if cred_obj is None:
        cred_obj = _resolve_series_credential(data, vat=vat, cred_name=cred_name)
    settings = cred_obj.get("series_settings") if isinstance(cred_obj, dict) else None
    if not isinstance(settings, dict):
        return base_series
    mode = str(settings.get("mode") or "document").strip().lower()
    if mode != "custom":
        return base_series
    custom = settings.get("custom") if isinstance(settings, dict) else {}
    if not isinstance(custom, dict):
        custom = {}
    primary, fallback_key = _series_preference_keys(data)
    for key in filter(None, [primary, fallback_key]):
        val = custom.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return base_series

# --- Excel path helper: πάντα με VAT + fiscal_year ---
def excel_path_for(vat: Optional[str] = None, cred_name: Optional[str] = None) -> str:
    def update_issuer_name_in_excel(vat: str, mark: str, issuer_name: str):
        """
        Ενημερώνει το όνομα εκδότη στο Excel invoices (στο group excel path) για το συγκεκριμένο MARK.
        Αντικαθιστά το πεδίο 'issuer_name' ή 'Name_issuer' ή 'Name' στη γραμμή που ταιριάζει με το MARK.
        """
        try:
            import openpyxl
            excel_path = excel_path_for(vat)
            if not os.path.exists(excel_path):
                return False
            wb = openpyxl.load_workbook(excel_path)
            ws = wb.active
            # Βρες τις στήλες
            header = [str(cell.value).strip() if cell.value else "" for cell in next(ws.iter_rows(min_row=1, max_row=1))]
            col_mark = None
            col_name = None
            for idx, h in enumerate(header):
                if h.lower() in {"mark", "invoice_id", "id"}:
                    col_mark = idx
                if h.lower() in {"issuer_name", "name_issuer", "name"}:
                    col_name = idx
            if col_mark is None or col_name is None:
                return False
            updated = False
            for row in ws.iter_rows(min_row=2):
                cell_mark = str(row[col_mark].value).strip() if row[col_mark].value else ""
                if cell_mark == str(mark).strip():
                    row[col_name].value = issuer_name
                    updated = True
            if updated:
                wb.save(excel_path)
                return True
            return False
        except Exception as e:
            log.exception(f"Failed to update issuer name in excel for mark {mark}: {e}")
            return False
    """
    Return path to per-vat excel file: DATA_DIR/excel/<safe_vat>_<fiscal_year>_invoices.xlsx
    Fallback: if vat/cred_name missing return DEFAULT_EXCEL_FILE.
    """
    excel_dir = group_path("excel")
    os.makedirs(excel_dir, exist_ok=True)

    # resolve fiscal year
    fy = None
    try:
        getter = globals().get("get_active_fiscal_year")
        if callable(getter):
            fy = getter()
    except Exception:
        fy = None
    if fy is None:
        from datetime import datetime
        fy = datetime.now().year

    if vat:
        try:
            safe_vat = secure_filename(str(vat))
        except Exception:
            safe_vat = str(vat)
        fname = f"{safe_vat}_{fy}_invoices.xlsx"
        return os.path.join(excel_dir, fname)

    if cred_name:
        try:
            safe_name = secure_filename(str(cred_name))
        except Exception:
            safe_name = str(cred_name)
        fname = f"{safe_name}_{fy}_invoices.xlsx"
        return os.path.join(excel_dir, fname)

    # fallback
    return DEFAULT_EXCEL_FILE


def _atomic_write(path, data_text):
    dirn = os.path.dirname(path)
    os.makedirs(dirn, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dirn, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data_text)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except Exception: pass

def _load_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_json(path, obj):
    txt = json.dumps(obj, ensure_ascii=False, indent=2)
    _atomic_write(path, txt)

def _match_row_by_mark(df, mark):
    """
    Προσπαθεί να βρει γραμμή στο dataframe df όπου κάποια από τις πιθανές στήλες που
    αντιστοιχούν σε 'mark' ταιριάζει με την τιμή mark.
    Επιστρέφει (found_bool, row_dict_or_None, column_name_used_or_None)
    """
    possible_cols = [
        "mark", "MARK", "Mark",
        "invoice_id", "invoiceId", "id",
        "Αριθμός", "Αριθμός Μητρώου", "Αριθμός Μητρώου Παρ", "Α/Α",
        "αριθμος", "α/α"
    ]
    for col in possible_cols:
        if col in df.columns:
            # ανάγνωση ως string για ασφαλή σύγκριση
            matches = df[df[col].astype(str).str.strip() == str(mark).strip()]
            if not matches.empty:
                # επιστρέφουμε την πρώτη αντιστοιχία ως dict
                return True, matches.iloc[0].to_dict(), col
    # Γενικός fallback: ψάξε σε όλες τις στήλες που είναι string-like
    str_cols = [c for c in df.columns if df[c].dtype == 'object' or df[c].dtype == 'string']
    for c in str_cols:
        matches = df[df[c].astype(str).str.strip() == str(mark).strip()]
        if not matches.empty:
            return True, matches.iloc[0].to_dict(), c
    return False, None, None

def get_active_credential_from_session() -> Optional[Dict]:
    name = session.get("active_credential")
    return get_cred_by_name(name) if name else None

# NEW helper: epsilon per-vat path
def epsilon_file_path_for(vat: str) -> str:
    epsilon_dir = group_path("epsilon")
    os.makedirs(epsilon_dir, exist_ok=True)
    return os.path.join(epsilon_dir, secure_filename(f"{vat}_epsilon_invoices.json"))

# New function: build epsilon from invoices.json (used when epsilon file missing)
def build_epsilon_from_invoices(vat: str) -> List[Dict]:
    """
    Δημιουργεί αρχική λίστα εγγραφών για το epsilon file (per-VAT) βασισμένη
    στο data/{vat}_invoices.json. Κάθε εγγραφή έχει:
      { "mark": "...", "AA": ..., "AFM": ..., "lines": [ {id, description, amount, vat, category:''}, ... ] }
    """
    invoices_file = get_customer_docs_file(vat)
    invoices = json_read(invoices_file) if os.path.exists(invoices_file) else []
    epsilon_list: List[Dict] = []

    def pick(src: dict, *keys, default=""):
        for k in keys:
            if k in src and src.get(k) not in (None, ""):
                return src.get(k)
        return default

    for doc in invoices:
        mark = str(pick(doc, "mark", "MARK", "Mark", default="")).strip()
        if not mark:
            mark = str(pick(doc, "identifier", "id", default="")).strip()
        if not mark:
            continue
        vat_doc = pick(doc, "AFM", "AFM_issuer", default="")
        aa = pick(doc, "AA", "aa", default="")
        raw_lines = doc.get("lines") or doc.get("Lines") or doc.get("Positions") or []
        prepared = []
        for idx, raw in enumerate(raw_lines):
            line_id = raw.get("id") or raw.get("line_id") or raw.get("LineId") or f"{mark}_l{idx}"
            description = pick(raw, "description", "desc", "Description", "name", "Name") or ""
            amount = pick(raw, "amount", "lineTotal", "net", "value", default="")
            vat_rate = pick(raw, "vat", "vatRate", "vatPercent", "vatAmount", default="")
            prepared.append({
                "id": line_id,
                "description": description,
                "amount": amount,
                "vat": vat_rate,
                "category": ""   # αρχικά κενό, user θα το συμπληρώσει
            })
        epsilon_list.append({
            "mark": mark,
            "AA": aa,
            "AFM": vat_doc,
            "lines": prepared
        })
    return epsilon_list

def load_epsilon_cache_for_vat(vat: str):
    """
    ΜΟΝΟ διαβάζει το DATA_DIR/epsilon/<vat>_epsilon_invoices.json.
    - Αν δεν υπάρχει: επιστρέφει [].
    - Αν είναι άδειο/χαλασμένο: επιστρέφει [].
    ΔΕΝ κάνει auto-build από Excel ή άλλα αρχεία.
    """
    try:
        eps_dir = group_path("epsilon")
        os.makedirs(eps_dir, exist_ok=True)
        path = os.path.join(eps_dir, f"{vat}_epsilon_invoices.json")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception:
                log.exception("load_epsilon_cache_for_vat: JSON decode error -> return []")
                return []
        if isinstance(data, list):
            return data
        # old shape dict -> convert to list of dict values
        out = []
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, dict):
                    out.append(v)
        return out
    except Exception:
        log.exception("load_epsilon_cache_for_vat: unexpected error")
        return []

def save_epsilon_cache_for_vat(vat: str, data: List[Dict]):
    path = epsilon_file_path_for(vat)
    try:
        json_write(path, data)
    except Exception:
        log.exception("Could not write epsilon cache for %s", vat)


def _pfloat_any(v) -> float:
    try:
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if not s:
            return 0.0
        s = s.replace(' ', '')
        if ',' in s and '.' in s:
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '.')
        return float(s)
    except Exception:
        return 0.0


def _build_table_rows_from_epsilon(vat: str, fiscal_year: Optional[int] = None) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    eps = load_epsilon_cache_for_vat(str(vat or '')) or []
    cred = get_cred_by_vat(str(vat or '').strip()) or {}
    category_labels = _category_labels_for_client(cred)

    def _map_category_label(raw_value: Any) -> str:
        raw = str(raw_value or '').strip()
        if not raw:
            return ''
        parts = [p.strip() for p in raw.split(',') if str(p or '').strip()]
        mapped: List[str] = []
        seen: set[str] = set()
        for part in parts or [raw]:
            key = str(part).strip()
            if not key:
                continue
            lbl = category_labels.get(key) or category_labels.get(key.lower()) or key
            if lbl in seen:
                continue
            seen.add(lbl)
            mapped.append(lbl)
        return ', '.join(mapped)

    selected_year = None
    try:
        if fiscal_year is not None:
            selected_year = int(fiscal_year)
    except Exception:
        selected_year = None

    for rec in eps:
        if not isinstance(rec, dict):
            continue

        # Apply active fiscal-year filtering to list rows (same UX as expenses export).
        if selected_year is not None:
            issue_date_raw = str(rec.get('issueDate') or rec.get('issue_date') or '').strip()
            issue_year = parse_year_from_date_string(issue_date_raw)
            if issue_year is None:
                try:
                    issue_year = int(str(rec.get('issue_year') or '').strip())
                except Exception:
                    issue_year = None
            if issue_year != selected_year:
                continue

        lines = rec.get('lines') if isinstance(rec.get('lines'), list) else []
        net = _pfloat_any(rec.get('totalNetValue') or rec.get('net') or rec.get('total_net'))
        vat_val = _pfloat_any(rec.get('totalVatAmount') or rec.get('vat') or rec.get('total_vat'))
        total = _pfloat_any(rec.get('totalValue') or rec.get('total') or rec.get('total_amount'))
        if (net == 0.0 and vat_val == 0.0) and lines:
            for ln in lines:
                if not isinstance(ln, dict):
                    continue
                net += _pfloat_any(ln.get('amount') or ln.get('lineTotal') or ln.get('total'))
                vat_val += _pfloat_any(ln.get('vat') or ln.get('vatRate') or ln.get('vatAmount'))
            if total == 0.0:
                total = net + vat_val
        if total == 0.0 and (net or vat_val):
            total = net + vat_val

        mark = str(rec.get('mark') or rec.get('MARK') or '').strip()
        issue_type = str(rec.get('type_name') or rec.get('type') or '').strip()
        issue_type_l = issue_type.lower()
        is_receipt = bool(rec.get('is_receipt')) or ('αποδ' in issue_type_l) or ('receipt' in issue_type_l)

        mtype = str(rec.get('mtype') or rec.get('invoice_mtype') or rec.get('receipt_mtype') or '').strip()
        auto_cash_payment = bool(rec.get('_auto_cash_payment'))

        line_categories = set()
        for ln in lines:
            if not isinstance(ln, dict):
                continue
            cat = str(ln.get('category') or '').strip().lower()
            if cat:
                line_categories.add(cat)

        has_supplier_cash_mirror = (
            ('προμηθευτής_λιανικής' in line_categories and 'ταμείο' in line_categories)
            or ('προμηθευτης_λιανικης' in line_categories and 'ταμειο' in line_categories)
        )

        # Hide cash movements from list_inner for both receipts and invoices.
        is_cash_movement = (
            auto_cash_payment
            or has_supplier_cash_mirror
            or issue_type in {'9.3'}
            or 'ταμεια' in issue_type_l
            or 'ταμει' in issue_type_l
            or mtype == '14'
        )
        if is_cash_movement:
            continue
        mapped_issue_type = map_invoice_type_label(issue_type)
        tipo_excel = 'ΑΠΟΔΕΙΞΗ' if is_receipt else (mapped_issue_type or 'ΤΙΜΟΛΟΓΙΟ')
        characteristic_category = str(
            rec.get('χαρακτηρισμός')
            or rec.get('χαρακτηρισμος')
            or rec.get('characteristic')
            or rec.get('category')
            or rec.get('classification')
            or ''
        ).strip()
        characteristic_category = _map_category_label(characteristic_category)
        if not characteristic_category and lines:
            line_categories = []
            for ln in lines:
                if not isinstance(ln, dict):
                    continue
                line_cat = str(
                    ln.get('category')
                    or ln.get('χαρακτηρισμός')
                    or ln.get('χαρακτηρισμος')
                    or ln.get('characteristic')
                    or ''
                ).strip()
                if not line_cat:
                    continue
                mapped_line_cat = _map_category_label(line_cat)
                if mapped_line_cat and mapped_line_cat not in line_categories:
                    line_categories.append(mapped_line_cat)
            characteristic_category = ', '.join(line_categories)

        row = {
            'MARK': mark,
            'ΑΦΜ': str(rec.get('AFM_issuer') or rec.get('AFM') or vat or '').strip(),
            'Επωνυμία': str(rec.get('Name_issuer') or rec.get('Name') or '').strip(),
            'Σειρά': str(rec.get('series') or '').strip(),
            'Αριθμός': str(rec.get('number') or rec.get('AA') or rec.get('aa') or rec.get('progressive_aa') or '').strip(),
            'Ημερομηνία': str(rec.get('issueDate') or rec.get('issue_date') or '').strip(),
            'Είδος': tipo_excel,
            'Κατηγορία Χαρακτηρισμού': characteristic_category,
            'Καθαρή Αξία': f"{net:.2f}".replace('.', ','),
            'ΦΠΑ': f"{vat_val:.2f}".replace('.', ','),
            'Σύνολο': f"{total:.2f}".replace('.', ','),
        }
        rows.append(row)

    return rows


def _render_table_html_for_vat(vat: str, with_checkbox_value: bool = True):
    import pandas as pd
    rows = _build_table_rows_from_epsilon(vat, fiscal_year=get_active_fiscal_year())
    if not rows:
        return "", False, ""

    df = pd.DataFrame(rows).fillna("").astype(str)
    if "MARK" in df.columns:
        if with_checkbox_value:
            checkboxes = df["MARK"].apply(lambda v: f'<input type="checkbox" name="delete_mark" value="{str(v)}">')
        else:
            checkboxes = ['<input type="checkbox" name="delete_mark" />'] * len(df)
        df.insert(0, "✓", checkboxes)

    table_html = df.to_html(classes="summary-table", index=False, escape=False)
    table_html = table_html.replace(
        "<th>✓</th>",
        '<th><input type="checkbox" id="selectAll" title="Επιλογή όλων"></th>'
    )
    table_html = table_html.replace("<td>", '<td><div class="cell-wrap">').replace("</td>", "</div></td>")
    table_html = strip_server_totals(table_html)
    return table_html, True, ""

# στο app.py — κάτω από get_active_credential_from_session()
@app.context_processor
def inject_active_credential():
    """
    Εισάγει αυτόματα στα templates:
      - active_credential: όνομα credential ή None
      - active_credential_vat: ΑΦΜ του active credential (ή empty string)
      - app_settings: γενικές ρυθμίσεις εφαρμογής (φορτώνονται από SETTINGS_FILE)
      - user_role: ρόλος του τρέχοντος χρήστη (admin ή member) στο active group
    """
    active = get_active_credential_from_session()
    name = active.get("name") if active else None
    vat = active.get("vat") if active else ""
    
    # Load settings (fall back to empty dict)
    try:
        settings = load_settings() or {}
    except Exception:
        log.exception("Could not load settings for context processor")
        settings = {}

    # Resolve active group ONCE to avoid multiple DB hits
    active_grp = None
    try:
        from auth import get_active_group
        active_grp = get_active_group()
    except Exception:
        pass

    # Get user role from active group
    user_role = "member"  # default
    try:
        from flask_login import current_user
        if getattr(current_user, 'is_authenticated', False) and active_grp:
            role = current_user.role_for_group(active_grp)
            if role in ('admin', 'member'):
                user_role = role
            else:
                user_role = 'member'
    except Exception as e:
        log.warning(f"[auth] Failed to determine user_role: {e}")
        user_role = "member"

    try:
        active_year = get_active_fiscal_year()
    except Exception:
        active_year = None
    
    active_group_name = getattr(active_grp, 'name', None) if active_grp else None

    # compute a display-friendly username (strip trailing _<id> appended for uniqueness)
    try:
        from flask_login import current_user as _cu
        import re as _re
        if getattr(_cu, 'is_authenticated', False):
            _uname = getattr(_cu, 'username', '') or ''
            display_username = _re.sub(r'_(\d+)$', '', _uname)
        else:
            display_username = None
    except Exception:
        display_username = None

    return dict(
        active_credential=name,
        active_credential_vat=vat,
        app_settings=settings,
        user_role=user_role,
        active_group=active_group_name,
        active_year=active_year,
        ADMIN_USER_ID=ADMIN_USER_ID if 'ADMIN_USER_ID' in globals() else 0,
        display_username=display_username,
    )



# ---------------- Validation helper ----------------
def normalize_input_date_to_iso(s: str):
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.datetime.strptime(s, "%d/%m/%Y")
        return dt.date().isoformat()
    except ValueError:
        return None

# ---------------- safe render ----------------
def safe_render(template_name, **ctx):
    try:
        return render_template(template_name, **ctx)
    except Exception as e:
        tb = traceback.format_exc()
        log.error("Template rendering failed for %s: %s\n%s", template_name, str(e), tb)
        debug = os.getenv("FLASK_DEBUG", "0") == "1"
        body = "<h2>Template error</h2><p>" + escape(str(e)) + "</p>"
        if debug:
            body += "<pre>" + escape(tb) + "</pre>"
        return body

# ---------------- Routes ----------------
@app.route("/icons/<path:filename>")
def serve_icons(filename):
    """Serve files from the icons directory"""
    icons_dir = os.path.join(os.path.dirname(__file__), 'icons')
    return send_file(os.path.join(icons_dir, filename))

@app.route("/")
@monitor_resources('home')
def home():
    return safe_render("nav.html", active_page="home")


@app.route('/terms')
@monitor_resources('terms_page')
def terms_page():
    """Terms of Service page - publicly accessible"""
    from datetime import datetime
    return render_template("terms.html", current_date=datetime.now().strftime("%B %Y"))


@app.route('/privacy')
@monitor_resources('privacy_page')
def privacy_page():
    """Privacy Policy & GDPR page - publicly accessible"""
    from datetime import datetime
    return render_template("privacy.html", current_date=datetime.now().strftime("%B %Y"))


@app.route('/get_fiscal_year', methods=['GET'])
@monitor_resources('route_get_fiscal_year')
def route_get_fiscal_year():
    """
    GET -> return current fiscal year if present:
    { exists: bool, fiscal_year: int|null }
    """
    y = get_active_fiscal_year()
    return jsonify(exists=(y is not None), fiscal_year=y), 200

@app.route('/set_fiscal_year', methods=['POST'])
@monitor_resources('route_set_fiscal_year')
def route_set_fiscal_year():
    """
    POST JSON or form: { fiscal_year: 2025 } or { year: 2025 }
    Returns JSON { success: bool, fiscal_year: int|null, message: str }
    """
    try:
        # accept JSON or form
        data = request.get_json(silent=True) or request.form or {}
        fy = data.get('fiscal_year') or data.get('year') or None
        if fy is None:
            return jsonify(success=False, message='Missing fiscal_year'), 400
        try:
            fy_int = int(fy)
        except Exception:
            return jsonify(success=False, message='Invalid fiscal_year'), 400

        ok = set_active_fiscal_year(fy_int)
        if not ok:
            return jsonify(success=False, message='Could not persist fiscal year'), 500
        return jsonify(success=True, fiscal_year=fy_int, message='Fiscal year updated'), 200
    except Exception:
        try:
            log.exception("Failed in set_fiscal_year")
        except Exception:
            pass
        return jsonify(success=False, message='Server error'), 500

# --- helpers για upsert / merge epsilon invoices -------------------------------
import uuid
from collections import OrderedDict

def _normalize_val(v):
    try:
        return "" if v is None else str(v).strip()
    except Exception:
        return ""

def _make_id_inv():
    return uuid.uuid4().hex

def _write_json_atomic(path, obj):
    """Atomic write (utf-8, indent=2)."""
    import tempfile
    dirn = os.path.dirname(path)
    os.makedirs(dirn, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_json_", dir=dirn)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise

def _upsert_epsilon_invoice(new_doc: dict):
    """
    Insert or update a detailed invoice record into the proper data/epsilon/*_epsilon_invoices.json.
    - Matches by mark (preferred) or AA.
    - If it finds an existing placeholder it replaces/merges it and preserves id_inv (or creates one).
    - If not found, it creates a per-vat file using AFM_issuer/AFM (if available) or 'unknown'.
    Returns (path_written, id_inv).
    """
    epsilon_dir = group_path("epsilon")
    os.makedirs(epsilon_dir, exist_ok=True)

    mark = _normalize_val(new_doc.get("mark") or new_doc.get("MARK"))
    aa = _normalize_val(new_doc.get("AA") or new_doc.get("aa"))
    target_vat = _normalize_val(new_doc.get("AFM_issuer") or new_doc.get("AFM") or new_doc.get("issuer_vat"))

    # build candidate list: prefer per-vat file if target_vat present, then all others
    candidates = []
    if target_vat:
        candidates.append(os.path.join(epsilon_dir, f"{secure_filename(target_vat)}_epsilon_invoices.json"))
    for fname in sorted(os.listdir(epsilon_dir)):
        if not fname.endswith("_epsilon_invoices.json"):
            continue
        full = os.path.join(epsilon_dir, fname)
        if full not in candidates:
            candidates.append(full)

    # helper to extract list container & container_key info
    def _extract_items_and_container(raw):
        if isinstance(raw, list):
            return raw, True, None, raw  # items, is_root_list, key, root_container
        if isinstance(raw, dict):
            for k in ("items", "invoices", "data", "records"):
                if k in raw and isinstance(raw[k], list):
                    return raw[k], False, k, raw
            # maybe single invoice dict
            if any(k in raw for k in ("mark", "AA", "AFM", "AFM_issuer")):
                return [raw], False, None, raw
        return [], False, None, raw

    # try to find & replace/merge
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            # ignore unreadable files
            continue

        items, is_list, container_key, root_container = _extract_items_and_container(raw)
        if not items:
            continue

        changed = False
        for idx, it in enumerate(items):
            try:
                it_mark = _normalize_val(it.get("mark") or it.get("MARK"))
                it_aa = _normalize_val(it.get("AA") or it.get("aa"))
                # match exact or substring (covers small formatting diffs)
                matched = False
                if mark and it_mark and (mark == it_mark or mark in it_mark or it_mark in mark):
                    matched = True
                elif aa and it_aa and aa == it_aa:
                    matched = True

                if not matched:
                    continue

                # found matching existing item -> merge/replace
                existing = dict(it) if isinstance(it, dict) else {}
                # preserve existing id_inv if present, else take from new_doc, else create one
                id_inv = _normalize_val(existing.get("id_inv") or new_doc.get("id_inv") or new_doc.get("id") or "")
                if not id_inv:
                    id_inv = _make_id_inv()

                # merge: new_doc fields override existing ones; keep any remaining existing keys if not present in new_doc
                merged = dict(existing)
                merged.update(new_doc)  # new_doc wins
                merged["id_inv"] = id_inv

                # ensure id_inv appears BEFORE 'lines' key in JSON order
                new_ordered = OrderedDict()
                inserted = False
                for k, v in list(merged.items()):
                    if k == "lines" and not inserted:
                        new_ordered["id_inv"] = id_inv
                        inserted = True
                    new_ordered[k] = v
                if not inserted:
                    # append id_inv at start (or end - we put at start to be "before lines")
                    od = OrderedDict()
                    od["id_inv"] = id_inv
                    for k,v in new_ordered.items():
                        od[k] = v
                    new_ordered = od

                # replace the item in the list
                items[idx] = dict(new_ordered)
                changed = True
                # write back updated container
                if changed:
                    if is_list:
                        to_write = items
                    else:
                        if container_key:
                            root_container[container_key] = items
                            to_write = root_container
                        else:
                            # single dict root replaced by this new item
                            to_write = items[0]
                    _write_json_atomic(path, to_write)
                    try:
                        log.info("_upsert_epsilon_invoice: updated %s (mark=%s AA=%s id_inv=%s)", path, mark, aa, id_inv)
                    except Exception:
                        pass
                    return path, id_inv

            except Exception:
                continue

    # not found anywhere -> create per-vat file (prefer AFM_issuer/AFM), else 'unknown'
    safe_vat = secure_filename(target_vat) if target_vat else "unknown"
    new_path = os.path.join(epsilon_dir, f"{safe_vat}_epsilon_invoices.json")
    id_inv = _normalize_val(new_doc.get("id_inv") or new_doc.get("id") or "")
    if not id_inv:
        id_inv = _make_id_inv()

    # ensure id_inv before lines
    merged = dict(new_doc)
    merged["id_inv"] = id_inv
    new_ordered = OrderedDict()
    inserted = False
    for k, v in list(merged.items()):
        if k == "lines" and not inserted:
            new_ordered["id_inv"] = id_inv
            inserted = True
        new_ordered[k] = v
    if not inserted:
        od = OrderedDict()
        od["id_inv"] = id_inv
        for k,v in new_ordered.items():
            od[k] = v
        new_ordered = od

    # write single-element list to new_path (or append if file exists and is a list)
    try:
        if os.path.exists(new_path):
            # if file exists and is a list, load & append
            with open(new_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, list):
                raw.append(dict(new_ordered))
                _write_json_atomic(new_path, raw)
            elif isinstance(raw, dict):
                # try to append into detected container key if possible, else rewrite as list
                appended = False
                for k in ("items","invoices","data","records"):
                    if k in raw and isinstance(raw[k], list):
                        raw[k].append(dict(new_ordered))
                        _write_json_atomic(new_path, raw)
                        appended = True
                        break
                if not appended:
                    # convert to list form
                    _write_json_atomic(new_path, [dict(new_ordered)])
            else:
                _write_json_atomic(new_path, [dict(new_ordered)])
        else:
            _write_json_atomic(new_path, [dict(new_ordered)])
        try:
            log.info("_upsert_epsilon_invoice: created %s (mark=%s AA=%s id_inv=%s)", new_path, mark, aa, id_inv)
        except Exception:
            pass
        return new_path, id_inv
    except Exception:
        try:
            log.exception("_upsert_epsilon_invoice: write failed for %s", new_path)
        except Exception:
            pass
        return "", ""
# -------------------------------------------------------------------------------

# ---------- validation helpers ----------
def parse_date_str_to_utc(date_str):
    """
    Try parse a date string (ISO-ish or dd/mm/yyyy or yyyy-mm-dd).
    Returns a datetime in UTC (naive or tz aware converted to UTC) or None.
    """
    if not date_str:
        return None
    # try ISO first
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt = _dt.strptime(date_str, fmt)
            # naive -> assume local? we'll treat naive as UTC to be strict
            if dt.tzinfo is None:
                # treat as UTC for server-side canonicalization
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt
        except Exception:
            continue
    # last resort: try dateutil if available
    try:
        from dateutil import parser as _parser
        dt = _parser.parse(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return None

def is_date_within_fiscal_year(dt_utc, fiscal_year):
    """
    dt_utc: datetime with tzinfo=UTC
    fiscal_year: int (e.g. 2025)
    Assumes fiscal year = calendar year (Jan 1 - Dec 31).
    If your fiscal year differs, adapt start/end calculation here.
    """
    if not dt_utc or fiscal_year is None:
        return False
    start = _dt(fiscal_year, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = _dt(fiscal_year, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    return start <= dt_utc <= end

def validate_date_field_against_active_fiscal(date_str, field_name='date'):
    """
    Centralized server-side check. Returns (ok:bool, message:str).
    Use this in any route that receives date strings from user.
    """
    fiscal_year = get_active_fiscal_year()
    if fiscal_year is None:
        return True, "No active fiscal year set"  # if no selection, don't block
    dt = parse_date_str_to_utc(date_str)
    if dt is None:
        return False, f"Δεν αναγνώστηκε σωστά η ημερομηνία στο πεδίο {field_name}."
    if not is_date_within_fiscal_year(dt, fiscal_year):
        # produce message with allowed year info
        return False, f"Η επιλεγμένη χρήση είναι {fiscal_year}. Η ημερομηνία στο πεδίο {field_name} ({date_str}) δεν ανήκει στη χρήση αυτή."
    return True, "OK"
# --- NEW: safer GET that also returns profile_name ---
@app.route('/api/repeat_entry/get_v2', methods=['GET'])
@monitor_resources('api_repeat_entry_get_v2')
def api_repeat_entry_get_v2():
    """
    Επιστρέφει repeat_entry {enabled, mapping, profile_name} + expense_tags (χωρίς 'αποδειξάκια').
    Δεν πειράζει τίποτα από το υπάρχον /api/repeat_entry/get.
    Query param: ?vat=...
    """
    try:
        creds = read_credentials_list() or []
    except Exception:
        creds = []

    vat = (request.args.get('vat') or "").strip() or None
    session_cred = (get_active_credential_from_session() if 'get_active_credential_from_session' in globals() else {}) or {}
    if not vat and isinstance(session_cred, dict):
        vat = (session_cred.get('vat') or session_cred.get('afm') or "").strip() or None

    base_resp = {"ok": True,
                 "repeat_entry": {"enabled": False, "mapping": {}, "profile_name": ""},
                 "expense_tags": []}

    if not creds:
        if vat:
            base_resp["afm"] = vat
            base_resp["vat"] = vat
        return jsonify(base_resp)

    # find client record
    client_rec = None
    if 'find_active_client_index' in globals():
        try:
            idx = find_active_client_index(creds, vat=vat)
            if idx is not None and 0 <= idx < len(creds) and isinstance(creds[idx], dict):
                client_rec = creds[idx]
        except Exception:
            client_rec = None

    if client_rec is None:
        for c in creds:
            try:
                if str((c.get('vat') or c.get('afm') or '')).strip() == (vat or '').strip():
                    client_rec = c
                    break
            except Exception:
                continue

    if not client_rec:
        return jsonify(base_resp)

    try:
        repeat = client_rec.get('repeat_entry') or {}
    except Exception:
        repeat = {}

    tags = _list_invoice_categories(client_rec)
    labels = _category_labels_for_client(client_rec)

    resp = {
        "ok": True,
        "repeat_entry": repeat,
        "expense_tags": tags,
        "category_labels": labels,
    }
    if vat:
        resp["afm"] = vat
        resp["vat"] = vat
    return jsonify(resp)


@app.route('/api/repeat_entry/get', methods=['GET'])
@monitor_resources('api_repeat_entry_get')
def api_repeat_entry_get():
    """
    Επιστρέφει στοιχεία repeat_entry + expense_tags και -όταν είναι διαθέσιμο-
    το afm/vat του ενεργού πελάτη ώστε το frontend να το χρησιμοποιεί.
    Παράμετρος query: ?vat=... (προαιρετικό)
    """
    try:
        creds = read_credentials_list()
    except Exception:
        creds = None

    # default empty response structure
    base_resp = {"ok": True, "repeat_entry": {"enabled": False, "mapping": {}}, "expense_tags": []}

    # try to get vat from query param or from session active credential
    vat_param = request.args.get('vat') or None
    session_cred = (get_active_credential_from_session() or {}) if 'get_active_credential_from_session' in globals() else {}
    vat_from_session = session_cred.get('vat') if isinstance(session_cred, dict) else None
    vat = vat_param or vat_from_session

    if not creds:
        # still include any afm/vat from session if present
        afm_from_session = ""
        try:
            # try common keys
            if isinstance(session_cred, dict):
                afm_from_session = (session_cred.get('afm') or session_cred.get('vat') or "") or ""
            afm_from_session = str(afm_from_session).strip() if afm_from_session else ""
        except Exception:
            afm_from_session = ""
        resp = dict(base_resp)
        if afm_from_session:
            resp['afm'] = afm_from_session
            resp['vat'] = afm_from_session
        return jsonify(resp)

    # find index of active client if possible
    idx = find_active_client_index(creds, vat=vat) if 'find_active_client_index' in globals() else None
    if idx is None:
        # not found — still try to return session info if available
        afm_guess = ""
        try:
            if isinstance(session_cred, dict):
                afm_guess = (session_cred.get('afm') or session_cred.get('vat') or "") or ""
            afm_guess = str(afm_guess).strip() if afm_guess else ""
        except Exception:
            afm_guess = ""

        resp = dict(base_resp)
        # if we can extract expense_tags from session_cred, include them
        try:
            expense_tags = _list_invoice_categories(session_cred)
            if expense_tags:
                resp['expense_tags'] = expense_tags
                resp['category_labels'] = _category_labels_for_client(session_cred)
        except Exception:
            pass

        if afm_guess:
            resp['afm'] = afm_guess
            resp['vat'] = afm_guess
        return jsonify(resp)

    # we have creds and an index -> build response
    client_rec = creds[idx] if idx is not None and idx < len(creds) else None
    repeat_raw = (client_rec.get('repeat_entry') if isinstance(client_rec, dict) else {}) or {"enabled": False, "mapping": {}}
    repeat = _build_repeat_entry_payload(repeat_raw)
    repeat["enabled"] = bool((repeat_raw or {}).get("enabled"))

    # normalize expense_tags
    expense_tags = _list_invoice_categories(client_rec)
    category_labels = _category_labels_for_client(client_rec)

    # try to extract afm/vat from client_rec (many possible key names)
    afm_found = ""
    vat_found = ""
    try:
        if isinstance(client_rec, dict):
            # check common keys
            for k in ("afm", "AFM", "vat", "VAT", "vat_number", "vatNumber"):
                v = client_rec.get(k)
                if v:
                    val = str(v).strip()
                    if not afm_found and (k.lower().startswith('afm') or (len(val) == 9 and val.isdigit())):
                        afm_found = val
                    if not vat_found and (k.lower().startswith('vat') or (len(val) >= 8)):
                        vat_found = val
            # nested structures
            for nested in ("active_client", "client", "credential"):
                if not afm_found and isinstance(client_rec.get(nested), dict):
                    nc = client_rec.get(nested)
                    for k in ("afm", "AFM", "vat", "VAT", "vat_number"):
                        if k in nc and nc[k]:
                            afm_found = afm_found or str(nc[k]).strip()
                            vat_found = vat_found or str(nc[k]).strip()
            # as fallback use name fields (not AFM, but useful info)
            client_name = client_rec.get('client_name') or client_rec.get('name') or client_rec.get('company') or client_rec.get('client') or ""
        else:
            client_name = ""
    except Exception:
        afm_found = afm_found or ""
        vat_found = vat_found or ""
        client_name = client_name if 'client_name' in locals() else ""

    # fallback to session credential values if nothing found
    try:
        if not afm_found and isinstance(session_cred, dict):
            afm_found = (session_cred.get('afm') or session_cred.get('vat') or "") or afm_found
        if not vat_found and isinstance(session_cred, dict):
            vat_found = (session_cred.get('vat') or session_cred.get('afm') or "") or vat_found
    except Exception:
        pass

    afm_found = (str(afm_found).strip() or "")
    vat_found = (str(vat_found).strip() or "")

    resp = {
        "ok": True,
        "repeat_entry": repeat,
        "expense_tags": expense_tags,
        "category_labels": category_labels,
    }
    # include descriptive client object (don't leak secrets) - include some fields if present
    try:
        client_summary = {}
        if isinstance(client_rec, dict):
            for fld in ("client_name", "name", "company"):
                if fld in client_rec and client_rec.get(fld):
                    client_summary["name"] = client_rec.get(fld)
                    break
            # include provided vat/afm if present
            if afm_found:
                client_summary["afm"] = afm_found
            elif vat_found:
                client_summary["vat"] = vat_found
            # include original entry for debugging only when debug enabled (optional)
            if app.debug:
                client_summary["_raw"] = client_rec
        if client_summary:
            resp["client"] = client_summary
    except Exception:
        pass

    if afm_found:
        resp['afm'] = afm_found
    elif vat_found:
        resp['vat'] = vat_found

    # also return the vat param echoed (helpful for debugging)
    if vat_param:
        resp['_queried_vat'] = vat_param

    return jsonify(resp)

@app.post("/api/repeat_entry/save")
def api_repeat_entry_save():
    data = request.get_json(force=True) or {}

    vat = str(data.get("vat") or "").strip()
    enabled = bool(data.get("enabled"))
    mapping_in = data.get("mapping") or {}
    general_mapping_in = data.get("general_mapping") or {}
    # "" => Γενικό
    profile_name = (data.get("profile_name") or "").strip()
    # MTYPE για Γ Κατηγορία (διαχωρισμός τιμολογίων/αποδείξεων)
    invoice_mtype = (data.get("invoice_mtype") or "").strip()
    receipt_mtype = (data.get("receipt_mtype") or "").strip()

    # Επιτρέπουμε ΜΟΝΟ ποσοστά ΦΠΑ
    mapping = _normalize_repeat_mapping(mapping_in)

    # Έλεγχος για κενές επιλογές
    missing = [k for k in VAT_KEYS if not mapping[k]]
    if missing:
        return jsonify(ok=False, error="Συμπλήρωσε κατηγορία για " + ", ".join(missing) + "."), 400

    with CREDENTIALS_RW_LOCK:
        creds = read_credentials_list() or []
        idx = find_active_client_index(creds, vat=vat)
        if idx is None:
            return jsonify(ok=False, error="Πελάτης δεν βρέθηκε."), 404

        client = creds[idx] if isinstance(creds[idx], dict) else {}

        repeat = (client.get("repeat_entry") if isinstance(client, dict) else {}) or {}
        previous_general = _normalize_repeat_mapping(repeat.get("general_mapping"))
        previous_mapping = _normalize_repeat_mapping(repeat.get("mapping"))

        normalized_general = None
        if isinstance(general_mapping_in, dict):
            candidate = _normalize_repeat_mapping(general_mapping_in)
            if _is_complete_repeat_mapping(candidate):
                normalized_general = candidate

        if not profile_name:
            normalized_general = dict(mapping)
        if normalized_general is None:
            if _is_complete_repeat_mapping(previous_general):
                normalized_general = previous_general
            elif _is_complete_repeat_mapping(previous_mapping):
                normalized_general = previous_mapping
            else:
                normalized_general = dict(mapping)

        repeat.update({
            "enabled": enabled,
            "mapping": mapping,
            # Χρησιμοποιούμε το module datetime:
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            # ΠΑΝΤΑ γράφουμε το profile_name — κενό σημαίνει «Γενικό»
            "profile_name": profile_name,
            # Αποθηκεύουμε τα MTYPE ξεχωριστά για τιμολόγια και αποδείξεις
            "invoice_mtype": invoice_mtype,
            "receipt_mtype": receipt_mtype,  # Αποθηκεύουμε για τις αποδείξεις
            "general_mapping": normalized_general,
        })
        client["repeat_entry"] = repeat
        creds[idx] = client

        write_credentials_list(creds)

    try:
        _sync_repeat_entry_backend(
            vat,
            enabled=enabled,
            invoice_mtype=invoice_mtype,
            receipt_mtype=receipt_mtype,
        )
    except Exception:
        log.exception("api_repeat_entry_save: backend sync failed for VAT=%s", vat)

    expense_tags = _list_invoice_categories(client)
    labels = _category_labels_for_client(client)

    return jsonify(ok=True, repeat_entry=repeat, expense_tags=expense_tags, category_labels=labels)


@app.post("/api/validate_payment_mtype")
def api_validate_payment_mtype():
    """
    Ελέγχει αν το paymentMethodType ταιριάζει με το επιλεγμένο MTYPE.
    
    Payload:
        {
            "mark": "...",
            "vat": "...",
            "selected_mtype": "3.4.1"
        }
    
    Returns:
        {
            "ok": true,
            "warning": "..." (αν υπάρχει ασυμφωνία),
            "payment_method_type": "3",
            "expected_mtype": {"code": "3.4.2", "label": "..."},
            "selected_mtype": "3.4.1"
        }
    """
    try:
        data = request.get_json(force=True) or {}
        mark = str(data.get("mark") or "").strip()
        vat = str(data.get("vat") or "").strip()
        selected_mtype = str(data.get("selected_mtype") or "").strip()
        
        if not mark or not vat:
            return jsonify({"ok": False, "error": "Missing mark or vat"}), 400
        
        # Βρες το invoice από το customer file
        customer_file = get_customer_docs_file(vat)
        if not os.path.exists(customer_file):
            # Δεν υπάρχει αρχείο - δεν μπορούμε να ελέγξουμε
            return jsonify({"ok": True, "warning": None})
        
        invoices = json_read(customer_file) or []
        invoice = None
        for inv in invoices:
            if str(inv.get("mark", "")).strip() == mark:
                invoice = inv
                break
        
        if not invoice:
            # Δεν βρέθηκε το invoice - δεν μπορούμε να ελέγξουμε
            return jsonify({"ok": True, "warning": None})
        
        payment_method_type = str(invoice.get("paymentMethodType", "")).strip()
        if not payment_method_type:
            # Δεν υπάρχει paymentMethodType - δεν μπορούμε να ελέγξουμε
            return jsonify({"ok": True, "warning": None})
        
        # Ειδικός, ρυθμιζόμενος από settings: όταν paymentMethodType==3 (Μετρητά)
        # και ο χρήστης έχει επιλέξει article-style MTYPE (π.χ. '14','16') διαφορετικό
        # από τον ρυθμισμένο cash article-code → προτείνουμε την αλλαγή (special_case).
        if payment_method_type == "3":
            try:
                settings = load_settings() or {}
                cash_article_code = str(settings.get("article_movement_type_agoron_exodon_tameiaki") or settings.get("article_movement_type_tameiaki") or "").strip()
                cash_article_label = (
                    "Αγορών - Εξόδων Ταμειακή"
                    if settings.get("article_movement_type_agoron_exodon_tameiaki")
                    else "Ταμειακή"
                )
                if selected_mtype and "." not in selected_mtype and cash_article_code and selected_mtype != cash_article_code:
                    return jsonify({
                        "ok": True,
                        "warning": (
                            f"Προσοχή: Το παραστατικό έχει τρόπο πληρωμής Μετρητά αλλά επέλεξες κωδικό κίνησης '{selected_mtype}'. "
                            f"Συνιστάται να χρησιμοποιήσεις τον κωδικό κίνησης '{cash_article_code}' ({cash_article_label}). Θέλεις να αλλάξετε?"
                        ),
                        "payment_method_type": payment_method_type,
                        "expected_mtype": {"code": cash_article_code, "label": cash_article_label},
                        "selected_mtype": selected_mtype,
                        "special_case": True
                    })
            except Exception:
                # non-fatal — πέφτουμε πίσω στο γενικό αποτέλεσμα
                log.exception("validate_payment_mtype: special-case cash detection failed")

        # Ελεγξε αν ταιριάζει με το επιλεγμένο MTYPE
        expected = get_expected_mtype_for_payment(payment_method_type)
        
        # Special handling: for cash (3) allow the user-configured article-style MTYPE
        # (e.g. '42') to be treated as equivalent to the epsilon MTYPE ('3.4.2').
        if payment_method_type == "3":
            try:
                settings = load_settings() or {}
                cash_article_code = str(settings.get("article_movement_type_agoron_exodon_tameiaki") or settings.get("article_movement_type_tameiaki") or "").strip()
                cash_article_label = (
                    "Αγορών - Εξόδων Ταμειακή"
                    if settings.get("article_movement_type_agoron_exodon_tameiaki")
                    else "Ταμειακή"
                )
                if cash_article_code and selected_mtype and selected_mtype == cash_article_code:
                    # treat configured article-code as a valid match for cash
                    return jsonify({
                        "ok": True,
                        "warning": None,
                        "payment_method_type": payment_method_type,
                        "expected_mtype": {"code": cash_article_code, "label": cash_article_label, "matched": True},
                        "selected_mtype": selected_mtype
                    })
            except Exception:
                log.exception("validate_payment_mtype: post-expected matching check failed")

        if not expected["matched"]:
            # Άγνωστος τύπος πληρωμής
            return jsonify({
                "ok": True,
                "warning": f"Άγνωστος τύπος πληρωμής: {payment_method_type}. Παρακαλώ επιβεβαίωσε ότι το MTYPE είναι σωστό.",
                "payment_method_type": payment_method_type,
                "expected_mtype": expected,
                "selected_mtype": selected_mtype
            })
        
        if expected["code"] != selected_mtype:
            # Ασυμφωνία
            return jsonify({
                "ok": True,
                "warning": (
                    f"Προσοχή: Το παραστατικό έχει τύπο πληρωμής {payment_method_type} "
                    f"που συνήθως αντιστοιχεί σε MTYPE '{expected['label']}' ({expected['code']}), "
                    f"αλλά επέλεξες '{selected_mtype}'. Θέλεις να συνεχίσεις;"
                ),
                "payment_method_type": payment_method_type,
                "expected_mtype": expected,
                "selected_mtype": selected_mtype
            })
        

        
        # Όλα καλά
        return jsonify({
            "ok": True,
            "warning": None,
            "payment_method_type": payment_method_type,
            "expected_mtype": expected,
            "selected_mtype": selected_mtype
        })
        
    except Exception as e:
        log.exception("validate_payment_mtype failed")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/get_vat_name", methods=["POST"])
@monitor_resources('api_get_vat_name')
def api_get_vat_name():
    """
    Λαμβάνει ΑΦΜ και επιστρέφει το όνομα της επιχείρησης από VAT validator (VIES + Business Portal fallback).
    Χρησιμοποιείται όταν το scraping ή το client_db δεν έχουν το issuer_name.
    
    Request JSON: { "vat": "123456789" }
    Response: { "ok": true, "name": "Company Name", "vat": "123456789" } ή { "ok": false, "error": "..." }
    """
    try:
        data = request.get_json(silent=True) or {}
        vat = str(data.get("vat") or "").strip()
        
        if not vat:
            return jsonify({"ok": False, "error": "Missing VAT number"}), 400
        
        # Import validator
        try:
            from vat_validator import validate_greek_vat
        except ImportError as e:
            log.error("vat_validator not available: %s", e)
            return jsonify({"ok": False, "error": "VAT validator not available"}), 500
        
        # Call validator
        result = validate_greek_vat(vat)
        
        if result.get("valid") and result.get("name"):
            return jsonify({
                "ok": True,
                "name": result["name"],
                "vat": result["vat_number"],
                "address": result.get("address")
            })
        else:
            error_msg = result.get("error") or "VAT number not found or invalid"
            log.warning("VAT validation failed for %s: %s", vat, error_msg)
            return jsonify({
                "ok": False,
                "error": error_msg,
                "vat": vat
            }), 404
            
    except Exception as e:
        log.exception("api_get_vat_name failed")
        return jsonify({"ok": False, "error": str(e)}), 500


def _decode_data_url(raw: str) -> Optional[bytes]:
    if not raw:
        return None
    try:
        if raw.startswith("data:"):
            match = re.match(r"^data:(.*?);base64,(.*)$", raw, re.IGNORECASE)
            if not match:
                return None
            payload = match.group(2)
        else:
            payload = raw
        return base64.b64decode(payload)
    except Exception:
        current_app.logger.exception("Failed to decode QR data url")
        return None


@app.post("/api/qr/decode")
def api_qr_decode():
    """Αποκωδικοποιεί εικόνα QR και επιστρέφει raw string & MARK."""
    payload_bytes: Optional[bytes] = None
    filename = "capture.png"

    if "file" in request.files:
        f = request.files["file"]
        payload_bytes = f.read()
        filename = f.filename or filename
    else:
        data = request.get_json(silent=True) or {}
        raw = data.get("image") or data.get("dataUrl") or data.get("data")
        payload_bytes = _decode_data_url(raw)
        filename = data.get("filename") or filename

    if not payload_bytes:
        return jsonify({"ok": False, "error": "Δεν ελήφθη εικόνα"}), 400

    payloads = decode_qr_payloads(payload_bytes, filename) or []
    if not payloads:
        return jsonify({"ok": False, "error": "Δεν εντοπίστηκε QR κώδικας"}), 404

    sanitized_payloads = []
    for item in payloads:
        normalized = _normalize_scanned_value(item)
        sanitized_payloads.append(normalized or item)

    first = sanitized_payloads[0]
    mark = extract_mark(first)
    return jsonify({"ok": True, "raw": first, "mark": mark, "payloads": sanitized_payloads})


@app.post("/api/qr/remote/start")
def api_qr_remote_start():
    _purge_remote_sessions()
    owner = _ensure_remote_owner_token()
    payload = request.get_json(silent=True) or {}
    requested_mode = payload.get("mode")
    mode = _normalize_remote_mode(requested_mode)
    if requested_mode is None and payload.get("receipts"):
        mode = "receipts"

    repeat_flag = _parse_bool(payload.get("repeat_enabled"))
    repeat_enabled = bool(repeat_flag) if repeat_flag is not None else False
    auto_flag = _parse_bool(payload.get("auto_submit_enabled"))
    auto_submit_enabled = bool(auto_flag) if auto_flag is not None else False

    now = _remote_qr_now()
    session_id = secrets.token_urlsafe(9)
    push_secret = secrets.token_urlsafe(32)
    expires_at = now + REMOTE_QR_SESSION_TTL

    # attach current desktop user & active group if available so mobile does not need to login
    owner_user_id = None
    owner_group_name = None
    try:
        from flask_login import current_user
        if getattr(current_user, 'is_authenticated', False):
            owner_user_id = current_user.id
            try:
                from auth import get_active_group
                g = get_active_group()
                if g:
                    owner_group_name = g.name
            except Exception:
                owner_group_name = None
    except Exception:
        owner_user_id = None

    entry = {
        "id": session_id,
        "owner_token": owner,
        "owner_user_id": owner_user_id,
        "owner_group": owner_group_name,
        "push_secret": push_secret,
        "mode": mode,
        "created_at": now,
        "expires_at": expires_at,
        "last_seen": now,
        "attached": False,
        "attached_at": None,
        "payload": None,
        "version": 0,
        "last_delivered_version": 0,
        "owner_ip": request.remote_addr,
        "repeat_enabled": repeat_enabled,
        "auto_submit_enabled": auto_submit_enabled,
        "remote_last_seen": None,
        "summary_state": None,
        "summary_version": 0,
        "summary_last_delivered": 0,
        "control": None,
        "control_version": 0,
        "control_last_delivered": 0,
        "excel_updating": False,
        "excel_updating_until": None,
    }

    with REMOTE_QR_LOCK:
        while session_id in REMOTE_QR_SESSIONS:
            session_id = secrets.token_urlsafe(9)
            entry["id"] = session_id
        REMOTE_QR_SESSIONS[session_id] = entry

    connect_url = _build_external_url("mobile_qr_scanner", session=session_id, token=push_secret)
    qr_image = _generate_qr_data_uri(connect_url)

    owner_hint = _token_hint(owner)
    log.info(
        "remote.start session=%s mode=%s repeat=%s auto=%s owner=%s ip=%s",
        session_id,
        mode,
        repeat_enabled,
        auto_submit_enabled,
        owner_hint,
        request.remote_addr,
    )

    return jsonify({
        "ok": True,
        "session_id": session_id,
        "connect_url": connect_url,
        "qr_image": qr_image,
        "expires_at": expires_at.isoformat(),
        "expires_in": int((expires_at - now).total_seconds()),
        "mode": mode,
        "version": entry["version"],
        "repeat_enabled": repeat_enabled,
        "auto_submit_enabled": auto_submit_enabled,
    })


@app.get("/api/qr/remote/status")
def api_qr_remote_status():
    _purge_remote_sessions()
    owner = session.get("_remote_qr_owner")
    session_id = request.args.get("session_id") or request.args.get("session")
    if not session_id:
        return jsonify(ok=False, error="Λείπει το αναγνωριστικό συνεδρίας."), 400
    if not owner:
        return jsonify(ok=False, error="Η συνεδρία δεν είναι διαθέσιμη."), 404

    try:
        since = int(request.args.get("since", "0"))
    except (TypeError, ValueError):
        since = 0

    now = _remote_qr_now()
    with REMOTE_QR_LOCK:
        entry = REMOTE_QR_SESSIONS.get(session_id)
        if not entry or entry.get("owner_token") != owner:
            return jsonify(ok=False, error="Η συνεδρία δεν βρέθηκε."), 404

        expires_at = entry.get("expires_at")
        if expires_at and now > expires_at:
            REMOTE_QR_SESSIONS.pop(session_id, None)
            return jsonify(ok=False, error="Η συνεδρία έληξε.", expired=True), 410

        remote_last_seen = entry.get("remote_last_seen")
        if entry.get("attached") and isinstance(remote_last_seen, datetime.datetime):
            if now - remote_last_seen > REMOTE_QR_REMOTE_STALE:
                # μην διαγράφεις τη συνεδρία: κράτα την ζωντανή αλλά αποσύνδεσε το κινητό
                entry["attached"] = False
                entry["remote_last_seen"] = None

        entry["last_seen"] = now
        version = entry.get("version", 0)
        last_delivered = entry.get("last_delivered_version", 0)
        payload_data: Optional[Dict[str, Any]] = None

        if version and version > max(last_delivered, since):
            payload = entry.get("payload") or {}
            payload_data = {
                "raw": payload.get("raw", ""),
                "mark": payload.get("mark", ""),
                "is_url": bool(payload.get("is_url")),
                "pushed_at": payload.get("pushed_at"),
            }
            entry["last_delivered_version"] = version

        response = {
            "ok": True,
            "session_id": session_id,
            "attached": bool(entry.get("attached")),
            "mode": entry.get("mode") or "invoices",
            "version": version,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "repeat_enabled": bool(entry.get("repeat_enabled")),
            "auto_submit_enabled": bool(entry.get("auto_submit_enabled")),
        }

        # Signal processing lock to mobile (blocks scans while desktop saves)
        response["excel_updating"] = _is_remote_processing(entry)

    if payload_data:
        response["payload"] = payload_data

    remote_last_seen_val = entry.get("remote_last_seen")
    if isinstance(remote_last_seen_val, datetime.datetime):
        response["remote_last_seen"] = remote_last_seen_val.isoformat()

    summary_version = int(entry.get("summary_version") or 0)
    response["summary_version"] = summary_version
    # Always include the latest summary payload so mobile can mirror desktop state
    if entry.get("summary_state") is not None:
        response["summary_state"] = entry["summary_state"]

    control_version = entry.get("control_version") or 0
    if control_version:
        last_control = entry.get("control_last_delivered") or 0
        if control_version > last_control:
            if entry.get("control") is not None:
                response["control"] = entry["control"]
            response["control_version"] = control_version
            entry["control_last_delivered"] = control_version

    log.info(
        "remote.status session=%s owner=%s since=%s version=%s attached=%s",
        session_id,
        _token_hint(owner),
        since,
        entry.get("version"),
        bool(entry.get("attached")),
    )

    return jsonify(response)


@app.post("/api/qr/remote/close")
def api_qr_remote_close():
    _purge_remote_sessions()
    owner = session.get("_remote_qr_owner")
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or data.get("session")
    if not session_id or not owner:
        return jsonify(ok=True)

    with REMOTE_QR_LOCK:
        entry = REMOTE_QR_SESSIONS.get(session_id)
        if entry and entry.get("owner_token") == owner:
            REMOTE_QR_SESSIONS.pop(session_id, None)

    log.info("remote.close session=%s owner=%s", session_id, _token_hint(owner))
    return jsonify(ok=True)


@app.post("/api/qr/remote/attach")
def api_qr_remote_attach():
    _purge_remote_sessions()
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or data.get("session")
    token = data.get("token") or data.get("secret")
    mode = data.get("mode")
    repeat_flag = _parse_bool(data.get("repeat_enabled"))
    auto_flag = _parse_bool(data.get("auto_submit_enabled"))

    if not session_id or not token:
        return jsonify(ok=False, error="Λείπουν παράμετροι."), 400

    now = _remote_qr_now()
    with REMOTE_QR_LOCK:
        entry = REMOTE_QR_SESSIONS.get(session_id)
        if not entry:
            return jsonify(ok=False, error="Η συνεδρία δεν βρέθηκε."), 404
        if entry.get("push_secret") != token:
            return jsonify(ok=False, error="Μη έγκυρο διακριτικό."), 403

        expires_at = entry.get("expires_at")
        if expires_at and now > expires_at:
            REMOTE_QR_SESSIONS.pop(session_id, None)
            return jsonify(ok=False, error="Η συνεδρία έληξε.", expired=True), 410

        if mode is not None:
            entry["mode"] = _normalize_remote_mode(mode)

        if repeat_flag is not None:
            entry["repeat_enabled"] = repeat_flag
        if auto_flag is not None:
            entry["auto_submit_enabled"] = auto_flag
        entry["attached"] = True
        entry["attached_at"] = now
        entry["last_seen"] = now
        entry["remote_last_seen"] = now
        entry["mobile_agent"] = request.headers.get("User-Agent", "")
        entry["expires_at"] = now + REMOTE_QR_SESSION_TTL
        expires_at = entry.get("expires_at")

        response = {
            "ok": True,
            "mode": entry.get("mode") or "invoices",
            "expires_at": expires_at.isoformat() if expires_at else None,
            "repeat_enabled": bool(entry.get("repeat_enabled")),
            "auto_submit_enabled": bool(entry.get("auto_submit_enabled")),
        }

        # Include current processing state so mobile can block scans immediately on attach
        response["excel_updating"] = _is_remote_processing(entry)

        summary_version = int(entry.get("summary_version") or 0)
        response["summary_version"] = summary_version
        if entry.get("summary_state") is not None:
            response["summary_state"] = entry["summary_state"]

        remote_last_seen = entry.get("remote_last_seen")
        if isinstance(remote_last_seen, datetime.datetime):
            response["remote_last_seen"] = remote_last_seen.isoformat()

    log.info(
        "remote.attach session=%s mode=%s repeat=%s auto=%s",
        session_id,
        response.get("mode"),
        bool(response.get("repeat_enabled")),
        bool(response.get("auto_submit_enabled")),
    )

    return jsonify(response)


@app.post("/api/qr/remote/heartbeat")
def api_qr_remote_heartbeat():
    _purge_remote_sessions()
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or data.get("session")
    token = data.get("token") or data.get("secret")
    mode = data.get("mode")
    repeat_flag = _parse_bool(data.get("repeat_enabled"))
    auto_flag = _parse_bool(data.get("auto_submit_enabled"))
    if not session_id or not token:
        return jsonify(ok=False, error="Λείπουν παράμετροι."), 400

    now = _remote_qr_now()
    with REMOTE_QR_LOCK:
        entry = REMOTE_QR_SESSIONS.get(session_id)
        if not entry:
            return jsonify(ok=False, error="Η συνεδρία δεν βρέθηκε."), 404
        if entry.get("push_secret") != token:
            return jsonify(ok=False, error="Μη έγκυρο διακριτικό."), 403

        expires_at = entry.get("expires_at")
        if expires_at and now > expires_at:
            REMOTE_QR_SESSIONS.pop(session_id, None)
            return jsonify(ok=False, error="Η συνεδρία έληξε.", expired=True), 410

        entry["last_seen"] = now
        if mode is not None:
            entry["mode"] = _normalize_remote_mode(mode)
        if repeat_flag is not None:
            entry["repeat_enabled"] = repeat_flag
        if auto_flag is not None:
            entry["auto_submit_enabled"] = auto_flag
        entry["remote_last_seen"] = now
        entry["attached"] = True
        entry["expires_at"] = now + REMOTE_QR_SESSION_TTL
        expires_at = entry.get("expires_at")

        response = {
            "ok": True,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "mode": entry.get("mode") or "invoices",
            "repeat_enabled": bool(entry.get("repeat_enabled")),
            "auto_submit_enabled": bool(entry.get("auto_submit_enabled")),
        }

        response["excel_updating"] = _is_remote_processing(entry)

        summary_version = int(entry.get("summary_version") or 0)
        response["summary_version"] = summary_version
        if entry.get("summary_state") is not None:
            response["summary_state"] = entry["summary_state"]

        remote_last_seen = entry.get("remote_last_seen")
        if isinstance(remote_last_seen, datetime.datetime):
            response["remote_last_seen"] = remote_last_seen.isoformat()

    log.info(
        "remote.heartbeat session=%s repeat=%s auto=%s",
        session_id,
        bool(response.get("repeat_enabled")),
        bool(response.get("auto_submit_enabled")),
    )

    return jsonify(response)


@app.post("/api/qr/remote/update")
def api_qr_remote_update():
    _purge_remote_sessions()
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or data.get("session")
    if not session_id:
        return jsonify(ok=False, error="Λείπει το αναγνωριστικό συνεδρίας."), 400

    mode_present = "mode" in data
    repeat_flag = _parse_bool(data.get("repeat_enabled"))
    auto_flag = _parse_bool(data.get("auto_submit_enabled"))
    summary_present = "summary_state" in data
    if not mode_present and repeat_flag is None and not summary_present:
        return jsonify(ok=False, error="Δεν ελήφθη ενημέρωση.", field="missing"), 400

    token = data.get("token") or data.get("secret")
    desired_mode = _normalize_remote_mode(data.get("mode")) if mode_present else None
    owner = session.get("_remote_qr_owner")
    now = _remote_qr_now()

    with REMOTE_QR_LOCK:
        entry = REMOTE_QR_SESSIONS.get(session_id)
        if not entry:
            return jsonify(ok=False, error="Η συνεδρία δεν βρέθηκε."), 404

        authorized = False
        if owner and entry.get("owner_token") == owner:
            authorized = True
        elif token and entry.get("push_secret") == token:
            authorized = True

        if not authorized:
            return jsonify(ok=False, error="Μη εξουσιοδοτημένη αλλαγή."), 403

        summary_state = None
        if summary_present and owner and entry.get("owner_token") == owner:
            summary_state = _sanitize_summary_state(data.get("summary_state"))

        if desired_mode is not None:
            entry["mode"] = desired_mode
        if repeat_flag is not None:
            entry["repeat_enabled"] = repeat_flag
        if auto_flag is not None:
            entry["auto_submit_enabled"] = auto_flag
        if summary_present and summary_state is not None:
            entry["summary_state"] = summary_state
            entry["summary_version"] = (entry.get("summary_version") or 0) + 1
            entry["summary_last_delivered"] = 0
        entry["last_seen"] = now
        entry["expires_at"] = now + REMOTE_QR_SESSION_TTL
        expires_at = entry.get("expires_at")

    log.info(
        "remote.update session=%s owner=%s mode=%s repeat=%s auto=%s summary=%s",
        session_id,
        _token_hint(owner),
        entry.get("mode"),
        bool(entry.get("repeat_enabled")),
        bool(entry.get("auto_submit_enabled")),
        bool(summary_present),
    )

    return jsonify(
        ok=True,
        mode=(entry.get("mode") if desired_mode is None else desired_mode),
        repeat_enabled=bool(entry.get("repeat_enabled")),
        auto_submit_enabled=bool(entry.get("auto_submit_enabled")),
        expires_at=expires_at.isoformat() if expires_at else None,
        summary_version=entry.get("summary_version"),
    )


@app.post("/api/qr/remote/push")
def api_qr_remote_push():
    _purge_remote_sessions()
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or data.get("session")
    token = data.get("token") or data.get("secret")
    raw_value = (data.get("payload") or data.get("raw") or "").strip()
    mode = data.get("mode")
    repeat_flag = _parse_bool(data.get("repeat_enabled"))
    auto_flag = _parse_bool(data.get("auto_submit_enabled"))

    if not session_id or not token or not raw_value:
        return jsonify(ok=False, error="Λείπουν δεδομένα προς αποστολή."), 400

    now = _remote_qr_now()
    with REMOTE_QR_LOCK:
        entry = REMOTE_QR_SESSIONS.get(session_id)
        if not entry:
            return jsonify(ok=False, error="Η συνεδρία δεν βρέθηκε."), 404
        if entry.get("push_secret") != token:
            return jsonify(ok=False, error="Μη έγκυρο διακριτικό."), 403

        expires_at = entry.get("expires_at")
        if expires_at and now > expires_at:
            REMOTE_QR_SESSIONS.pop(session_id, None)
            return jsonify(ok=False, error="Η συνεδρία έληξε.", expired=True), 410

        normalized_value = _normalize_scanned_value(raw_value)
        mark = extract_mark(normalized_value) or extract_mark(raw_value) or ""
        is_url = bool(re.match(r"^https?://", normalized_value, re.IGNORECASE))
        payload = {
            "raw": normalized_value,
            "mark": mark,
            "is_url": is_url,
            "pushed_at": now.isoformat(),
            "source": "remote",
        }

        if mode is not None:
            entry["mode"] = _normalize_remote_mode(mode)
        if repeat_flag is not None:
            entry["repeat_enabled"] = repeat_flag
        if auto_flag is not None:
            entry["auto_submit_enabled"] = auto_flag

        entry["payload"] = payload
        entry["version"] = (entry.get("version") or 0) + 1
        entry["last_seen"] = now
        entry["attached"] = True
        entry["last_push_at"] = now
        entry["remote_last_seen"] = now
        entry["expires_at"] = now + REMOTE_QR_SESSION_TTL
        version = entry["version"]

        log.info(
            "remote.push session=%s mark=%s url=%s repeat=%s auto=%s",
            session_id,
            mark or "-",
            bool(is_url),
            bool(entry.get("repeat_enabled")),
            bool(entry.get("auto_submit_enabled")),
        )

    return jsonify(
        ok=True,
        mark=mark,
        is_url=is_url,
        version=version,
        repeat_enabled=bool(repeat_flag if repeat_flag is not None else entry.get("repeat_enabled")),
        auto_submit_enabled=bool(auto_flag if auto_flag is not None else entry.get("auto_submit_enabled")),
    )


@app.post("/api/qr/remote/control")
def api_qr_remote_control():
    _purge_remote_sessions()
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id") or data.get("session")
    token = data.get("token") or data.get("secret")
    command = data.get("command") or {}

    if not session_id or not token:
        return jsonify(ok=False, error="Λείπουν δεδομένα εντολής."), 400

    sanitized = _sanitize_remote_control(command)
    if sanitized is None:
        return jsonify(ok=False, error="Μη έγκυρη εντολή."), 400

    now = _remote_qr_now()
    with REMOTE_QR_LOCK:
        entry = REMOTE_QR_SESSIONS.get(session_id)
        if not entry:
            return jsonify(ok=False, error="Η συνεδρία δεν βρέθηκε."), 404
        if entry.get("push_secret") != token:
            return jsonify(ok=False, error="Μη έγκυρο διακριτικό."), 403

        expires_at = entry.get("expires_at")
        if expires_at and now > expires_at:
            REMOTE_QR_SESSIONS.pop(session_id, None)
            return jsonify(ok=False, error="Η συνεδρία έληξε.", expired=True), 410

        entry["last_seen"] = now
        entry["remote_last_seen"] = now
        entry["expires_at"] = now + REMOTE_QR_SESSION_TTL
        entry["attached"] = True
        entry["control"] = {
            **sanitized,
            "issued_at": now.isoformat(),
        }
        entry["control_version"] = (entry.get("control_version") or 0) + 1
        entry["control_last_delivered"] = 0

    log.info(
        "remote.control session=%s type=%s version=%s",
        session_id,
        sanitized.get("type"),
        entry.get("control_version"),
    )

    return jsonify(ok=True, control_version=entry.get("control_version"))


@app.get("/mobile/qr-scanner")
def mobile_qr_scanner():
    _purge_remote_sessions()
    session_id = request.args.get("session") or request.args.get("session_id") or request.args.get("id")
    token = request.args.get("token") or request.args.get("secret")

    if not session_id or not token:
        return (
            render_template(
                "mobile_qr_scanner.html",
                session_id=session_id or "",
                token=token or "",
                mode="invoices",
                expires_at="",
                repeat_enabled=False,
                auto_submit_enabled=False,
                active_year=None,
                error="Η συνεδρία δεν είναι διαθέσιμη.",
            ),
            400,
        )

    now = _remote_qr_now()

    with REMOTE_QR_LOCK:
        entry = REMOTE_QR_SESSIONS.get(session_id)
        if not entry:
            return (
                render_template(
                    "mobile_qr_scanner.html",
                    session_id=session_id,
                    token=token,
                    mode="invoices",
                    expires_at="",
                    repeat_enabled=False,
                    auto_submit_enabled=False,
                    active_year=None,
                    error="Η συνεδρία δεν βρέθηκε ή έληξε.",
                ),
                404,
            )
        if entry.get("push_secret") != token:
            return (
                render_template(
                    "mobile_qr_scanner.html",
                    session_id=session_id,
                    token=token,
                    mode="invoices",
                    expires_at="",
                    repeat_enabled=False,
                    auto_submit_enabled=False,
                    active_year=None,
                    error="Ο σύνδεσμος δεν είναι πλέον έγκυρος.",
                ),
                403,
            )

        expires_at = entry.get("expires_at")
        if expires_at and now > expires_at:
            REMOTE_QR_SESSIONS.pop(session_id, None)
            return (
                render_template(
                    "mobile_qr_scanner.html",
                    session_id=session_id,
                    token=token,
                    mode="invoices",
                    expires_at="",
                    repeat_enabled=False,
                    auto_submit_enabled=False,
                    active_year=None,
                    error="Η συνεδρία έληξε. Δημιούργησε νέο σύνδεσμο από τον υπολογιστή.",
                ),
                410,
            )

        entry["last_seen"] = now
        entry.setdefault("mobile_visits", 0)
        entry["mobile_visits"] += 1
        entry["mobile_agent"] = request.headers.get("User-Agent", "")
        mode = _normalize_remote_mode(entry.get("mode"))
        expires_iso = expires_at.isoformat() if expires_at else ""
        repeat_enabled = bool(entry.get("repeat_enabled"))

        # Get active fiscal year
        from epsilon_bridge_multiclient_strict import _read_active_fiscal_year
        active_year = _read_active_fiscal_year("data")

    return render_template(
        "mobile_qr_scanner.html",
        session_id=session_id,
        token=token,
        mode=mode,
        expires_at=expires_iso,
        repeat_enabled=repeat_enabled,
        auto_submit_enabled=bool(entry.get("auto_submit_enabled")),
        active_year=active_year,
        error=None,
    )






@app.route('/credentials/add', methods=['POST'])
@monitor_resources('credentials_add')
def credentials_add():
    # Only group admin may add credentials
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            flash('Δεν έχει επιλεγεί ενεργή ομάδα', 'error')
            return redirect(url_for('credentials'))
        if not getattr(current_user, 'is_authenticated', False) or current_user.role_for_group(grp) != 'admin':
            flash('Απαιτούνται δικαιώματα διαχειριστή για προσθήκη credentials', 'error')
            return redirect(url_for('credentials'))
    except Exception:
        flash('Αποτυχία ελέγχου δικαιωμάτων', 'error')
        return redirect(url_for('credentials'))

    credentials = load_credentials()
    name = request.form.get('name')
    vat = request.form.get('vat')
    user = request.form.get('user')
    key = request.form.get('key')
    book_category = request.form.get('book_category','Β')
    fpa_applicable = bool(request.form.get('fpa_applicable'))
    expense_tags = request.form.getlist('expense_tags')
    apodeixakia_type = request.form.get('apodeixakia_type', '').strip()
    apodeixakia_supplier = request.form.get('apodeixakia_supplier', '').strip()
    apodeixakia_other_expenses = request.form.get('apodeixakia_other_expenses')
    apodeixakia_other_expenses = True if apodeixakia_other_expenses in ('on', 'true', '1', 'yes') else False
    new_cred = {
        'name': name,
        'vat': vat,
        'user': user,
        'key': key,
        'book_category': book_category,
        'fpa_applicable': fpa_applicable,
        'expense_tags': expense_tags,
        'apodeixakia_type': apodeixakia_type,
        'apodeixakia_supplier': apodeixakia_supplier,
        'apodeixakia_other_expenses': apodeixakia_other_expenses,
        'custom_categories': [],
        'active': False
    }
    credentials.append(new_cred)
    save_credentials(credentials)
    # <-- added flash to ensure message appears if this route is used
    flash("Αποθηκεύτηκε", "success")
    return redirect(url_for('credentials'))




def _delete_credential_and_related_data(name: str):
    creds = load_credentials()
    credential = next((c for c in creds if c.get("name") == name), None)
    if not credential:
        return None, False, {"count": 0, "files": [], "failed": []}

    remaining = [c for c in creds if c.get("name") != name]
    save_credentials(remaining)

    was_active = False
    if session.get("active_credential") == name:
        session.pop("active_credential", None)
        was_active = True

    cleanup = delete_customer_data_files(credential.get("vat"))
    return credential, was_active, cleanup


@app.route('/credentials/delete/<name>', methods=['POST'])
@monitor_resources('credentials_delete_post')
def credentials_delete_post(name):
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    # Only group admin may delete credentials
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            if is_ajax:
                return jsonify({'status': 'error', 'error': 'no active group selected'}), 403
            flash('Δεν έχει επιλεγεί ενεργή ομάδα', 'error')
            return redirect(url_for('credentials'))
        if not getattr(current_user, 'is_authenticated', False) or current_user.role_for_group(grp) != 'admin':
            if is_ajax:
                return jsonify({'status': 'error', 'error': 'admin privileges required'}), 403
            flash('Απαιτούνται δικαιώματα διαχειριστή για διαγραφή credentials', 'error')
            return redirect(url_for('credentials'))
    except Exception:
        if is_ajax:
            return jsonify({'status': 'error', 'error': 'permission check failed'}), 500
        flash('Αποτυχία ελέγχου δικαιωμάτων', 'error')
        return redirect(url_for('credentials'))

    credential, was_active, cleanup = _delete_credential_and_related_data(name)
    if not credential:
        if is_ajax:
            return jsonify({'status': 'error', 'error': 'not found'}), 404
        flash(f"Το credential '{name}' δεν βρέθηκε", "error")
        return redirect(url_for('credentials'))

    if is_ajax:
        return jsonify({
            'status': 'ok',
            'deleted': name,
            'was_active': was_active,
            'data_files_removed': cleanup,
        }), 200

    suffix = ''
    if cleanup.get('count'):
        suffix = f" Αφαιρέθηκαν {cleanup['count']} αρχεία δεδομένων πελάτη."
    flash(f"Credential {name} διαγράφηκε.{suffix}", "success")
    if was_active:
        flash("Το credential αφαιρέθηκε επίσης από τα ενεργά.", "info")
    if cleanup.get('failed'):
        failed_count = len(cleanup.get('failed') or [])
        if failed_count:
            flash(f"Δεν ήταν δυνατή η διαγραφή {failed_count} αρχείων. Έλεγξε τα δικαιώματα πρόσβασης.", "error")
    return redirect(url_for('credentials'))

@app.route('/credentials/set_active', methods=['POST'])
@monitor_resources('credentials_set_active')
def credentials_set_active():
    active_name = request.form.get('active_name')
    credentials = load_credentials()
    for c in credentials:
        c['active'] = (c['name'] == active_name)
    save_credentials(credentials)
    # <-- added flash in case this route is used
    flash(f"Το ενεργό credential ορίστηκε σε {active_name}", "success")
    return redirect(url_for('credentials'))

@app.route('/credentials/save_settings', methods=['POST'])
@monitor_resources('credentials_save_settings')
def credentials_save_settings():
    # Only group admin may update settings
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            return jsonify({'status':'error','error':'no active group selected'}), 403
        if not getattr(current_user, 'is_authenticated', False) or current_user.role_for_group(grp) != 'admin':
            return jsonify({'status':'error','error':'admin privileges required'}), 403
    except Exception:
        return jsonify({'status':'error','error':'permission check failed'}), 500

    data = request.get_json()
    if data:
        save_settings(data)
        return jsonify({'status':'ok'})
    return jsonify({'status':'error'}), 400

@app.route('/api/validate_vat', methods=['POST'])
@monitor_resources('api_validate_vat')
def api_validate_vat():
    """
    Validate Greek VAT number using EU VIES service
    Expects JSON: { "vat": "123456789" }
    Returns: { "valid": bool, "name": str, "address": str, "error": str }
    """
    try:
        from vat_validator import validate_greek_vat
    except ImportError:
        log.warning("vat_validator module not available")
        return jsonify(valid=False, error="VAT validation service not available"), 200
    
    try:
        data = request.get_json(silent=True) or {}
        vat_number = data.get('vat', '').strip()
        
        if not vat_number:
            return jsonify(valid=False, error="VAT number is required"), 400
        
        result = validate_greek_vat(vat_number)
        return jsonify(result), 200
        
    except Exception as e:
        log.exception("VAT validation error")
        return jsonify(valid=False, error=f"Validation error: {str(e)}"), 500


@app.route('/api/save_receipt', methods=['POST'])
@monitor_resources('api_save_receipt')
def api_save_receipt():
    """
    Expects JSON { "summary": {...} }
    Writes ONLY to epsilon_invoices.json and to Excel.
    Returns JSON { ok: True, duplicate: Bool, message: str }
    """
    try:
        payload = request.get_json(silent=True) or {}
        summary = payload.get('summary') or {}
        if not summary:
            return jsonify(ok=False, error='no summary provided'), 400

        rec = _normalize_receipt_summary(summary)

        # Acquire JSON lock and check duplicates & append
        json_lock = FileLock(EPSILON_JSON_LOCK, timeout=10)
        with json_lock:
            epsilon_list = _read_epsilon_json()
            if _is_duplicate_in_epsilon(epsilon_list, rec):
                return jsonify(ok=True, duplicate=True, message='duplicate entry'), 200
            # append and write
            epsilon_list.append(rec)
            try:
                _write_epsilon_json(epsilon_list)
            except Exception as e:
                current_app.logger.exception("failed writing epsilon json: %s", e)
                return jsonify(ok=False, error='write epsilon json failed'), 500

        # Append to Excel with separate lock
        excel_lock = FileLock(EXCEL_FILE_LOCK, timeout=10)
        with excel_lock:
            ok_excel = _append_to_excel(rec)
            if not ok_excel:
                current_app.logger.warning("Excel append failed for record: %s", rec)
                # we proceed since epsilon json already saved - but inform client
                return jsonify(ok=True, duplicate=False, warning='excel_append_failed', message='saved to epsilon json but excel append failed'), 200

        return jsonify(ok=True, duplicate=False, message='saved'), 200

    except Exception as e:
        current_app.logger.exception("api_save_receipt error: %s", e)
        return jsonify(ok=False, error=str(e)), 500

@app.route('/upload_client_db', methods=['POST'])
@monitor_resources('upload_client_db')
def upload_client_db():
    """
    Accept multipart/form-data with field 'client_file' and save/update it in group DATA_DIR
    as client_db{.ext}. Existing rows are merged by AFM (new upload updates same AFM,
    unknown AFM rows are appended) and client_db.meta.json is updated.
    Returns JSON with upload/merge stats.
    """
    # Permission check: only admins can upload client_db
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            return jsonify(success=False, message='Δεν επιλέχθηκε ενεργή ομάδα.'), 403
        if not getattr(current_user, 'is_authenticated', False) or current_user.role_for_group(grp) != 'admin':
            return jsonify(success=False, message='Απαιτούνται δικαιώματα διαχειριστή για αυτή την ενέργεια.'), 403
    except Exception:
        return jsonify(success=False, message='Ο έλεγχος δικαιωμάτων απέτυχε.'), 403

    try:
        if 'client_file' not in request.files:
            return jsonify(success=False, message='Δεν βρέθηκε το πεδίο client_file στο αίτημα.'), 400

        f = request.files['client_file']
        if not f or not getattr(f, 'filename', '').strip():
            return jsonify(success=False, message='Δεν επιλέχθηκε αρχείο.'), 400

        uploaded_original_name = secure_filename(f.filename)
        _, ext = os.path.splitext(uploaded_original_name)
        ext = ext.lower()
        if ext not in ALLOWED_CLIENT_EXT:
            return jsonify(success=False, message='Μη επιτρεπτή επέκταση. Χρήση .xlsx, .xls ή .csv'), 400

        # Resolve target group base once.
        target_base = get_group_base_dir()
        os.makedirs(target_base, exist_ok=True)

        # Read upload once (header validation is done on DataFrame columns to reduce I/O).
        try:
            f.stream.seek(0)
            if ext in ['.xls', '.xlsx']:
                df_upload = pd.read_excel(f.stream, dtype=str)
            else:
                df_upload = pd.read_csv(f.stream, dtype=str)
            df_upload.fillna('', inplace=True)
        except Exception as e:
            log.exception('[Client DB Upload] Failed to read uploaded client_file')
            return jsonify(success=False, message=f'Σφάλμα κατά την ανάγνωση του αρχείου: {e}'), 500

        headers_set = {str(h).strip() for h in df_upload.columns}
        missing = sorted(list(REQUIRED_CLIENT_COLUMNS - headers_set))
        if missing:
            return jsonify(
                success=False,
                message='Λείπουν υποχρεωτικές στήλες.',
                missing_columns=missing,
                detected_columns=sorted(list(headers_set))
            ), 400

        # Determine existing client_db file (if any).
        existing_path = None
        existing_ext = None
        for existing in os.listdir(target_base):
            if not existing.startswith('client_db'):
                continue
            ext_candidate = os.path.splitext(existing)[1].lower()
            if ext_candidate in ALLOWED_CLIENT_EXT and '.bak.' not in existing and not existing.endswith('.bak'):
                existing_path = os.path.join(target_base, existing)
                existing_ext = ext_candidate
                break

        # Keep stable naming convention client_db{.ext}; if a client_db already exists, preserve its extension.
        final_ext = existing_ext or ext
        dest_name = f'client_db{final_ext}'
        dest_path = os.path.join(target_base, dest_name)

        # Merge strategy by AFM: upload updates existing AFM, new AFM appended.
        merge_source_rows = len(df_upload)
        merged_rows = merge_source_rows
        updated_clients = 0
        new_clients = 0
        if 'ΑΦΜ' in df_upload.columns:
            upload_afm = df_upload['ΑΦΜ'].astype(str).str.strip()
            upload_unique_afm = set(a for a in upload_afm if a)
        else:
            upload_unique_afm = set()

        if existing_path and os.path.exists(existing_path):
            try:
                if existing_ext in ['.xls', '.xlsx']:
                    df_existing = pd.read_excel(existing_path, dtype=str)
                else:
                    df_existing = pd.read_csv(existing_path, dtype=str)
                df_existing.fillna('', inplace=True)

                # Align schemas (keep all columns from both sources).
                all_columns = list(dict.fromkeys([*df_existing.columns.tolist(), *df_upload.columns.tolist()]))
                df_existing = df_existing.reindex(columns=all_columns, fill_value='')
                df_upload = df_upload.reindex(columns=all_columns, fill_value='')

                if 'ΑΦΜ' in df_existing.columns and 'ΑΦΜ' in df_upload.columns:
                    existing_afm = df_existing['ΑΦΜ'].astype(str).str.strip()
                    existing_afm_set = set(a for a in existing_afm if a)

                    updated_clients = len(upload_unique_afm & existing_afm_set)
                    new_clients = len(upload_unique_afm - existing_afm_set)

                    # Keep existing rows only for AFMs not re-uploaded, then append full uploaded set.
                    keep_mask = ~existing_afm.isin(upload_unique_afm)
                    df_merged = pd.concat([df_existing.loc[keep_mask], df_upload], ignore_index=True)
                else:
                    # Missing AFM in one of datasets: safe append fallback.
                    df_merged = pd.concat([df_existing, df_upload], ignore_index=True)
                    new_clients = len(upload_unique_afm)

                df_merged.fillna('', inplace=True)
                merged_rows = len(df_merged)
            except Exception:
                log.exception('[Client DB Upload] Failed reading existing client_db; fallback to uploaded file only')
                df_merged = df_upload.copy()
                new_clients = len(upload_unique_afm)
                updated_clients = 0
                merged_rows = len(df_merged)
        else:
            df_merged = df_upload.copy()
            new_clients = len(upload_unique_afm)
            updated_clients = 0

        # Rotate backups only after merge is ready.
        for existing in os.listdir(target_base):
            if not existing.startswith('client_db'):
                continue
            if '.bak.' in existing or existing.endswith('.bak'):
                try:
                    os.remove(os.path.join(target_base, existing))
                except Exception:
                    log.exception('Failed to remove old backup %s (continuing)', existing)

        if os.path.exists(dest_path):
            ts = _dt.utcnow().strftime('%Y%m%dT%H%M%SZ')
            backup_name = f"{dest_name}.bak.{ts}"
            try:
                os.rename(dest_path, os.path.join(target_base, backup_name))
            except Exception:
                log.exception('Failed to backup previous client_db %s (continuing)', dest_name)

        # If existing file had different extension, archive it too (single active canonical file remains).
        if existing_path and os.path.exists(existing_path) and os.path.abspath(existing_path) != os.path.abspath(dest_path):
            ts = _dt.utcnow().strftime('%Y%m%dT%H%M%SZ')
            try:
                os.rename(existing_path, os.path.join(target_base, f"{os.path.basename(existing_path)}.bak.{ts}"))
            except Exception:
                log.exception('Failed to archive previous client_db variant %s', existing_path)

        # Save merged dataset.
        try:
            if final_ext in ['.xls', '.xlsx']:
                if final_ext == '.xlsx':
                    df_merged.to_excel(dest_path, index=False, engine='openpyxl')
                else:
                    # Try legacy .xls writer; fallback to .xlsx if not available.
                    try:
                        df_merged.to_excel(dest_path, index=False)
                    except Exception:
                        log.warning('Legacy .xls writer unavailable, falling back to .xlsx')
                        final_ext = '.xlsx'
                        dest_name = f'client_db{final_ext}'
                        dest_path = os.path.join(target_base, dest_name)
                        df_merged.to_excel(dest_path, index=False, engine='openpyxl')
            else:
                df_merged.to_csv(dest_path, index=False, encoding='utf-8-sig')
        except Exception:
            log.exception('Failed to save merged client_db to %s', dest_path)
            return jsonify(success=False, message='Σφάλμα κατά την αποθήκευση του ενημερωμένου αρχείου.'), 500

        uploaded_at_iso = _dt.utcnow().replace(microsecond=0).isoformat() + 'Z'
        meta_extra = {
            'original_filename': uploaded_original_name,
            'storage_filename': dest_name,
            'total_rows': int(merged_rows),
            'source_rows': int(merge_source_rows),
            'new_clients': int(new_clients),
            'existing_clients': int(updated_clients),
            'merge_mode': 'upsert_by_afm'
        }
        try:
            # Keep canonical naming in metadata too.
            write_client_meta(dest_name, uploaded_at_iso, base_dir=target_base, extra_meta=meta_extra)
        except Exception:
            log.exception('[Client DB Upload] Failed to write client_db metadata (continuing anyway)')

        return jsonify(
            success=True,
            message=f'Αποθηκεύτηκε/ενημερώθηκε: {dest_name}',
            filename=dest_name,
            original_filename=uploaded_original_name,
            uploaded_at=uploaded_at_iso,
            detected_columns=sorted(list(headers_set)),
            total_rows=int(merged_rows),
            source_rows=int(merge_source_rows),
            new_clients=int(new_clients),
            existing_clients=int(updated_clients)
        ), 200

    except Exception:
        log.exception('Unhandled exception in upload_client_db')
        return jsonify(success=False, message='Εσωτερικό σφάλμα server.'), 500


@app.route('/upload_chart_of_accounts', methods=['POST'])
@monitor_resources('upload_chart_of_accounts')
def upload_chart_of_accounts():
    """
    Upload λογιστικού σχεδίου για Γ Κατηγορία.
    Αναμένει αρχείο Excel με στήλες: Κωδικός, Περιγραφή, Ποσοστό ΦΠΑ, Λογαριασμός ΦΠΑ
    """
    return _upload_chart_of_accounts_impl('G')

@app.route('/upload_chart_of_accounts_b', methods=['POST'])
@monitor_resources('upload_chart_of_accounts_b')
def upload_chart_of_accounts_b():
    """
    Upload λογιστικού σχεδίου για Β Κατηγορία.
    Αναμένει αρχείο Excel με στήλες: Κωδικός, Περιγραφή, Ποσοστό ΦΠΑ, Λογαριασμός ΦΠΑ
    """
    return _upload_chart_of_accounts_impl('B')

def _upload_chart_of_accounts_impl(category='G'):
    """
    Κοινή λογική upload λογιστικού σχεδίου.
    category: 'B' ή 'G'
    """
    category_label = 'Γ Κατηγορία' if category == 'G' else 'Β Κατηγορία'
    dest_name = 'chart_of_accounts_g.xlsx' if category == 'G' else 'chart_of_accounts_b.xlsx'
    
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            log.warning("[CoA Upload %s] No active group selected", category)
            return jsonify(ok=False, error='Δεν επιλέχθηκε ενεργή ομάδα.'), 403
        if not getattr(current_user, 'is_authenticated', False):
            log.warning("[CoA Upload %s] User not authenticated", category)
            return jsonify(ok=False, error='Απαιτείται σύνδεση.'), 403
        log.info("[CoA Upload %s] User: %s, Group: %s", category, current_user.username, grp.name)
    except Exception as e:
        log.exception("[CoA Upload %s] Permission check failed", category)
        return jsonify(ok=False, error='Ο έλεγχος δικαιωμάτων απέτυχε.'), 403

    try:
        if 'coa_file' not in request.files:
            log.warning("[CoA Upload %s] No file in request", category)
            return jsonify(ok=False, error='Δεν βρέθηκε το αρχείο.'), 400

        f = request.files['coa_file']
        vat = request.form.get('vat', '').strip()
        log.info("[CoA Upload %s] VAT: %s, File: %s", category, vat, f.filename if f else 'None')
        
        if not f or not getattr(f, 'filename', '').strip():
            log.warning("[CoA Upload] Empty file or filename")
            return jsonify(ok=False, error='Δεν επιλέχθηκε αρχείο.'), 400

        filename = secure_filename(f.filename)
        ext = os.path.splitext(filename)[1].lower()
        if ext not in ['.xls', '.xlsx']:
            log.warning("[CoA Upload] Invalid extension: %s", ext)
            return jsonify(ok=False, error='Επιτρέπονται μόνο αρχεία .xls ή .xlsx'), 400

        # --- ΕΛΕΓΧΟΣ ΣΤΗΛΩΝ ΠΡΙΝ ΤΗΝ ΑΠΟΘΗΚΕΥΣΗ ---
        log.info("[CoA Upload] Reading Excel file...")
        try:
            # Important: Ensure stream is at the beginning
            f.stream.seek(0)
            df = pd.read_excel(f.stream, dtype=str)
            df.fillna('', inplace=True)
            log.info("[CoA Upload] Excel read successfully: %d rows, %d columns", len(df), len(df.columns))
        except Exception as e:
            log.exception("Failed to read chart of accounts file")
            return jsonify(ok=False, error=f'Σφάλμα ανάγνωσης: {e}'), 500

        # Έλεγχος απαιτούμενων στηλών
        required_cols = {'Κωδικός', 'Περιγραφή', 'Ποσοστό ΦΠΑ', 'Λογαριασμός ΦΠΑ'}
        headers_set = {str(h).strip() for h in df.columns}
        log.info("[CoA Upload] Columns found: %s", sorted(headers_set))
        missing = required_cols - headers_set
        if missing:
            log.warning("[CoA Upload] Missing columns: %s", missing)
            return jsonify(
                ok=False, 
                error=f'Λείπουν υποχρεωτικές στήλες: {", ".join(sorted(missing))}',
                missing_columns=sorted(list(missing)),
                detected_columns=sorted(list(headers_set))
            ), 400
        
        account_count = len(df)
        if account_count == 0:
            log.warning("[CoA Upload] File has no accounts")
            return jsonify(ok=False, error='Το αρχείο δεν περιέχει λογαριασμούς.'), 400

        log.info("[CoA Upload] Validation passed: %d accounts", account_count)

        # --- BACKUP SYSTEM (όπως το client_db) ---
        # ΣΗΜΑΝΤΙΚΟ: Το CoA είναι GROUP-WIDE, όχι per-VAT!
        target_base = os.path.join(BASE_DIR, 'data', grp.data_folder or '')
        os.makedirs(target_base, exist_ok=True)
        log.info("[CoA Upload %s] Target directory (GROUP-WIDE): %s", category, target_base)
        
        # Ένα αρχείο ανά κατηγορία (Β/Γ) για όλη την ομάδα
        dest_path = os.path.join(target_base, dest_name)

        # 1) Διαγραφή παλιών backups (μόνο για αυτή την κατηγορία)
        backup_prefix = dest_name.replace('.xlsx', '')
        for existing in os.listdir(target_base):
            if existing.startswith(backup_prefix) and ('.bak.' in existing or existing.endswith('.bak')):
                try:
                    os.remove(os.path.join(target_base, existing))
                    log.info("Removed old CoA %s backup: %s", category, existing)
                except Exception:
                    log.exception("Failed to remove old CoA %s backup %s", category, existing)

        # 2) Backup του υπάρχοντος αρχείου (αν υπάρχει)
        if os.path.exists(dest_path):
            ts = _dt.utcnow().strftime('%Y%m%dT%H%M%SZ')
            backup_name = f"{dest_name}.bak.{ts}"
            backup_path = os.path.join(target_base, backup_name)
            try:
                os.rename(dest_path, backup_path)
                log.info("Backed up previous CoA %s: %s -> %s", category, dest_name, backup_name)
            except Exception:
                log.exception("Failed to backup previous CoA %s %s", category, dest_name)

        # 3) Αποθήκευση νέου αρχείου
        try:
            f.stream.seek(0)
            f.save(dest_path)
            log.info("Saved new CoA %s to: %s", category, dest_path)
        except Exception as e:
            log.exception("Failed to save CoA %s file", category)
            return jsonify(ok=False, error=f'Σφάλμα κατά την αποθήκευση: {e}'), 500
        
        uploaded_at = _dt.utcnow().replace(microsecond=0).isoformat() + 'Z'
        
        # 4) Αποθήκευση metadata - με σαφές error handling
        meta_path = os.path.join(target_base, f'{dest_name}.meta.json')
        if account_count <= 0:
            log.error("[CoA Upload] CRITICAL: account_count is %d after validation (should be > 0)", account_count)
            return jsonify(ok=False, error='Εσωτερικό σφάλμα: Μη έγκυρο πλήθος λογαριασμών.'), 500
            
        meta = {
            'filename': dest_name,
            'original_filename': filename,
            'uploaded_at': uploaded_at,
            'account_count': account_count,
            'category': category,
            'scope': 'group-wide',  # Κοινόχρηστο για όλη την ομάδα
            'columns': sorted(list(headers_set))
        }
        try:
            with open(meta_path, 'w', encoding='utf-8') as mf:
                json.dump(meta, mf, ensure_ascii=False, indent=2)
            log.info("[CoA Upload] Metadata saved: %s", meta_path)
            log.info("[CoA Upload] Metadata content: account_count=%d", account_count)
        except Exception as e:
            log.exception("[CoA Upload] CRITICAL: Failed to write metadata to %s", meta_path)
            # Even if metadata fails, don't lose the file - try to continue
            log.error("[CoA Upload] Will return success anyway since file was saved, but metadata write failed")
            return jsonify(ok=False, error=f'Σφάλμα αποθήκευσης μεταδεδομένων: {e}'), 500

        return jsonify(
            ok=True,
            message=f'Το λογιστικό σχέδιο {category_label} αποθηκεύτηκε επιτυχώς (κοινόχρηστο για όλη την ομάδα)',
            filename=dest_name,
            original_filename=filename,
            uploaded_at=uploaded_at,
            account_count=account_count,
            category=category,
            detected_columns=sorted(list(headers_set))
        ), 200

    except Exception:
        log.exception("Unhandled exception in upload_chart_of_accounts %s", category)
        return jsonify(ok=False, error='Εσωτερικό σφάλμα server.'), 500


@app.route('/remove_chart_of_accounts', methods=['POST'])
@monitor_resources('remove_chart_of_accounts')
def remove_chart_of_accounts():
    """Αφαίρεση λογιστικού σχεδίου Γ Κατηγορίας"""
    return _remove_chart_of_accounts_impl('G')

@app.route('/remove_chart_of_accounts_b', methods=['POST'])
@monitor_resources('remove_chart_of_accounts_b')
def remove_chart_of_accounts_b():
    """Αφαίρεση λογιστικού σχεδίου Β Κατηγορίας"""
    return _remove_chart_of_accounts_impl('B')

def _remove_chart_of_accounts_impl(category='G'):
    """Κοινή λογική αφαίρεσης λογιστικού σχεδίου"""
    dest_name = 'chart_of_accounts_g.xlsx' if category == 'G' else 'chart_of_accounts_b.xlsx'
    
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            return jsonify(ok=False, error='Δεν επιλέχθηκε ενεργή ομάδα.'), 403
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify(ok=False, error='Απαιτείται σύνδεση.'), 403
    except Exception:
        return jsonify(ok=False, error='Ο έλεγχος δικαιωμάτων απέτυχε.'), 403

    try:
        target_base = os.path.join(BASE_DIR, 'data', grp.data_folder or '')
        # Category-specific file
        dest_path = os.path.join(target_base, dest_name)
        meta_path = os.path.join(target_base, f'{dest_name}.meta.json')
        
        if os.path.exists(dest_path):
            os.remove(dest_path)
        if os.path.exists(meta_path):
            os.remove(meta_path)
        
        return jsonify(ok=True), 200

    except Exception:
        log.exception("Unhandled exception in remove_chart_of_accounts %s", category)
        return jsonify(ok=False, error='Εσωτερικό σφάλμα server.'), 500


@app.route('/get_chart_of_accounts_status', methods=['GET'])
def get_chart_of_accounts_status():
    """Επιστρέφει το status του λογιστικού σχεδίου Γ Κατηγορίας"""
    return _get_chart_of_accounts_status_impl('G')

@app.route('/get_chart_of_accounts_status_b', methods=['GET'])
def get_chart_of_accounts_status_b():
    """Επιστρέφει το status του λογιστικού σχεδίου Β Κατηγορίας"""
    return _get_chart_of_accounts_status_impl('B')

def _get_chart_of_accounts_status_impl(category='G'):
    """Κοινή λογική status λογιστικού σχεδίου"""
    dest_name = 'chart_of_accounts_g.xlsx' if category == 'G' else 'chart_of_accounts_b.xlsx'
    
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            return jsonify(ok=False, error='Δεν επιλέχθηκε ενεργή ομάδα.'), 403
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify(ok=False, error='Απαιτείται σύνδεση.'), 403
    except Exception:
        return jsonify(ok=False, error='Ο έλεγχος δικαιωμάτων απέτυχε.'), 403

    try:
        target_base = os.path.join(BASE_DIR, 'data', grp.data_folder or '')
        # Category-specific file
        dest_path = os.path.join(target_base, dest_name)
        meta_path = os.path.join(target_base, f'{dest_name}.meta.json')
        
        log.info("[CoA Status %s] Checking: %s", category, dest_path)
        log.info("[CoA Status %s] Metadata path: %s", category, meta_path)
        
        if not os.path.exists(dest_path):
            log.info("[CoA Status %s] File not found: %s", category, dest_path)
            return jsonify(ok=True, exists=False, category=category), 200
        
        # Load metadata
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, 'r', encoding='utf-8') as mf:
                    meta = json.load(mf)
                log.info("[CoA Status %s] Metadata loaded: account_count=%s", category, meta.get('account_count'))
            except Exception as e:
                log.error("[CoA Status %s] Failed to read metadata: %s", category, e)
        else:
            log.warning("[CoA Status %s] Metadata file not found: %s", category, meta_path)
        
        account_count = meta.get('account_count', 0)
        log.info("[CoA Status %s] Returning: exists=True, account_count=%d", category, account_count)
        
        return jsonify(
            ok=True,
            exists=True,
            category=category,
            filename=meta.get('filename', dest_name),
            uploaded_at=meta.get('uploaded_at', ''),
            account_count=account_count
        ), 200

    except Exception:
        log.exception("Unhandled exception in get_chart_of_accounts_status %s", category)
        return jsonify(ok=False, error='Εσωτερικό σφάλμα server.'), 500


# -------- Chart of Accounts search API (suggestions for account inputs) --------
# Simple in-memory cache per category to avoid reading Excel on every request
_COA_CACHE = {
    'G': {'mtime': None, 'rows': None, 'path': None},
    'B': {'mtime': None, 'rows': None, 'path': None},
}

def _get_coa_file_path(category: str):
    dest_name = 'chart_of_accounts_g.xlsx' if category == 'G' else 'chart_of_accounts_b.xlsx'
    try:
        from auth import get_active_group
        grp = get_active_group()
        base = os.path.join(BASE_DIR, 'data', (grp.data_folder or '') if grp else '')
    except Exception:
        base = get_group_base_dir() if 'get_group_base_dir' in globals() else os.path.join(BASE_DIR, 'data')
    return os.path.join(base, dest_name)

def _load_coa_rows(category: str):
    """
    Return list of dicts {code, name} for chart of accounts of given category.
    Applies basic format filtering per category.
    Caches by file mtime.
    """
    category = 'G' if str(category).upper().startswith('G') else 'B'
    cache = _COA_CACHE[category]
    path = _get_coa_file_path(category)
    cache['path'] = path
    if not os.path.exists(path):
        cache['mtime'] = None
        cache['rows'] = []
        return []
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = None
    if cache['rows'] is not None and cache['mtime'] == mtime:
        return cache['rows'] or []
    # (Re)load
    rows = []
    try:
        df = pd.read_excel(path, dtype=str)
        df.fillna('', inplace=True)
        # Columns may be named in Greek; tolerate variants
        code_col = None
        name_col = None
        vat_col = None
        for c in df.columns:
            s = str(c).strip().lower()
            if not code_col and ('κωδ' in s or s == 'code' or 'λογαριασμ' in s):
                code_col = c
            if not name_col and ('περιγραφ' in s or s == 'name' or s == 'description'):
                name_col = c
            if not vat_col and ('ποσοστό φπα' in s or 'ποσοστο φπα' in s or 'vat' == s or 'vat rate' in s):
                vat_col = c
        if not code_col:
            # fallback to first column
            code_col = df.columns[0]
        # Build rows
        for _, r in df.iterrows():
            code = str(r.get(code_col, '')).strip()
            name = str(r.get(name_col, '')).strip() if name_col else ''
            # Parse VAT rate if available
            vat_rate = None
            if vat_col:
                raw = str(r.get(vat_col, '')).strip()
                raw = raw.replace('%','').replace(',','.')
                try:
                    # keep integer buckets commonly used
                    v = float(raw) if raw else None
                    if v is not None:
                        if abs(v - round(v)) < 1e-9:
                            vat_rate = int(round(v))
                        else:
                            vat_rate = v
                except Exception:
                    vat_rate = None
            if not code:
                continue
            # normalize code format (ensure dashes exist if digits provided)
            digits = ''.join(ch for ch in code if ch.isdigit())
            if category == 'G':
                # Expect 10 digits -> xx-xx-xx-xxxx
                if len(digits) != 10:
                    # Skip non-standard codes for suggestions
                    continue
                code_fmt = f"{digits[0:2]}-{digits[2:4]}-{digits[4:6]}-{digits[6:10]}"
            else:
                # B expects 6 digits -> xx-xxxx
                if len(digits) != 6:
                    continue
                code_fmt = f"{digits[0:2]}-{digits[2:6]}"
            rows.append({'code': code_fmt, 'name': name, 'vat_rate': vat_rate})
    except Exception:
        try:
            app.logger.exception("Failed to read CoA file for category %s", category)
        except Exception:
            pass
        rows = []
    cache['rows'] = rows
    cache['mtime'] = mtime
    return rows

@app.get('/api/coa/search')
def api_coa_search():
    """
    Query chart of accounts for suggestions.
    Query params: category=B|G (default G), q=search string, limit=int (default 20)
    Returns: { ok: bool, exists: bool, results: [ { code, name } ], total: int }
    """
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            return jsonify({'ok': False, 'error': 'no_group'}), 403
        if not getattr(current_user, 'is_authenticated', False):
            return jsonify({'ok': False, 'error': 'unauthorized'}), 403
    except Exception:
        return jsonify({'ok': False, 'error': 'auth_failed'}), 403

    category = (request.args.get('category') or 'G').strip().upper()
    category = 'G' if category.startswith('G') or category == 'Γ' else 'B'
    q = (request.args.get('q') or '').strip()
    vat_q_raw = (request.args.get('vat') or '').strip()
    vat_filter = None
    try:
        if vat_q_raw:
            # accept numbers like 24 or strings '24%'
            vat_filter = float(vat_q_raw.replace('%','').replace(',','.'))
            if abs(vat_filter - round(vat_filter)) < 1e-9:
                vat_filter = int(round(vat_filter))
    except Exception:
        vat_filter = None
    try:
        limit = int(request.args.get('limit') or 20)
        if limit <= 0:
            limit = 20
        if limit > 100:
            limit = 100
    except Exception:
        limit = 20

    # Load rows (cached)
    rows = _load_coa_rows(category)
    path = _COA_CACHE[category]['path']
    if not path or not os.path.exists(path):
        return jsonify({'ok': True, 'exists': False, 'results': [], 'total': 0})

    if not q:
        # Return first N sorted by code, optionally filter by VAT
        base = rows
        if vat_filter is not None:
            # Include rows with matching VAT or empty VAT
            base = [it for it in base if (it.get('vat_rate') == vat_filter) or (it.get('vat_rate') in (None, ''))]
        results = sorted(base, key=lambda x: x.get('code',''))[:limit]
        return jsonify({'ok': True, 'exists': True, 'results': results, 'total': len(base)})

    q_low = q.lower()
    q_digits = ''.join(ch for ch in q if ch.isdigit())

    def match(item):
        code = item.get('code','')
        name = item.get('name','')
        if q_digits:
            code_digits = ''.join(ch for ch in code if ch.isdigit())
            if q_digits in code_digits:
                return True
        if q_low in code.lower():
            return True
        if name and q_low in name.lower():
            return True
        return False

    # Pre-filter by VAT rate when provided
    base_rows = rows
    if vat_filter is not None:
        # Include rows with matching VAT or empty VAT
        base_rows = [it for it in base_rows if (it.get('vat_rate') == vat_filter) or (it.get('vat_rate') in (None, ''))]
    filtered = [it for it in base_rows if match(it)]
    # Prioritize startswith on code, then name contains
    def sort_key(it):
        code = it.get('code','')
        name = it.get('name','')
        starts = 0
        if code.lower().startswith(q_low):
            starts = -2
        elif name.lower().startswith(q_low):
            starts = -1
        return (starts, code, name)
    filtered.sort(key=sort_key)
    results = filtered[:limit]
    return jsonify({'ok': True, 'exists': True, 'results': results, 'total': len(filtered)})


@app.route("/api/profiles", methods=["GET"])
def api_profiles_list():
    profs, options = _profiles_get_for_active()
    invoice_profiles = _filter_char_profiles_by_mode(profs or [], "invoices")
    return jsonify({"ok": True, "profiles": invoice_profiles, "options": options})


@app.route("/api/profiles/save", methods=["POST"])
def api_profiles_save():
    payload = request.get_json(force=True, silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "missing name"}), 400
    mapping = payload.get("map") or {}

    # sanitize for invoices: never allow 'αποδειξακια'
    sanitized = {}
    for k,v in (mapping or {}).items():
        if str(v).strip().lower() == "αποδειξακια":
            continue
        sanitized[k] = v

    profs, _ = _profiles_get_for_active()
    pid = (payload.get("id") or "").strip()
    if pid:
        found = False
        for p in profs:
            if str(p.get("id") or "") == pid:
                p["name"] = name
                p["map"]  = sanitized
                found = True
                break
        if not found:
            profs.append({"id": pid, "name": name, "map": sanitized})
    else:
        import uuid
        profs.append({"id": str(uuid.uuid4()), "name": name, "map": sanitized})

    _profiles_set_for_active(profs)
    return jsonify({"ok": True})

@app.route("/api/profiles/delete", methods=["POST"])
def api_profiles_delete():
    payload = request.get_json(force=True, silent=True) or {}
    pid = (payload.get("id") or "").strip()
    if not pid:
        return jsonify({"ok": False, "error": "missing id"}), 400
    profs, _ = _profiles_get_for_active()
    profs = [p for p in profs if str(p.get("id") or "") != pid]
    _profiles_set_for_active(profs)
    return jsonify({"ok": True})


@app.route("/api/last_fetch_date", methods=["GET"])
def api_last_fetch_date():
    """
    Get the last fetch date for a credential.
    Query params: credential (credential name)
    Returns: { last_fetch_date: str|null (ISO 8601) }
    """
    try:
        credential_name = (request.args.get("credential") or "").strip()
        credential_vat = (request.args.get("vat") or "").strip()
        if not credential_name and not credential_vat:
            return jsonify({"last_fetch_date": None}), 400

        fetch_key = _get_fetch_tracking_key(credential_name, credential_vat)
        last_date = get_last_fetch_date(fetch_key, only_meta=True) if fetch_key else None
        if not last_date and fetch_key:
            # Fallback to activity log when metadata is missing.
            last_date = get_last_fetch_date(fetch_key, only_meta=False)
        if not last_date and credential_name and fetch_key != credential_name:
            # Backward compatibility: older installs may have written by credential name.
            last_date = get_last_fetch_date(credential_name, only_meta=True)
        if not last_date and credential_name:
            last_date = get_last_fetch_date(credential_name, only_meta=False)
        
        formatted = _format_last_fetch_date_for_display(last_date)
        
        return jsonify({"last_fetch_date": formatted, "last_fetch_raw": last_date})
    except Exception as e:
        log.exception("api_last_fetch_date error")
        return jsonify({"error": str(e)}), 500

# --- client_db_info route ---
@app.route('/client_db_info', methods=['GET'])
def client_db_info():
    """
    Return JSON with metadata about current client_db (if exists).
    { exists: bool, filename: str|null, uploaded_at: str|null, total_rows: int, new_rows: int, updated_rows: int }
    """
    try:
        # Always resolve against the active group first to avoid cross-group metadata mismatches.
        target_base = get_group_base_dir()
        requested_group = (request.args.get('group') or '').strip()
        try:
            from flask_login import current_user
            if requested_group and getattr(current_user, 'is_authenticated', False):
                user_groups = getattr(current_user, 'groups', [])
                grp = next((g for g in user_groups if g.name == requested_group), None)
                if not grp:
                    return jsonify({'ok': False, 'error': 'Δεν έχετε πρόσβαση στην επιλεγμένη ομάδα.'}), 403
                target_base = os.path.join(BASE_DIR, 'data', grp.data_folder or '')
        except Exception:
            pass

        meta = read_client_meta(base_dir=target_base)
        counts = {'total_rows': 0, 'new_rows': 0, 'updated_rows': 0}

        if meta:
            # Prefer precomputed counts from metadata (upload path stores these for low-resource environments).
            if any(k in meta for k in ('total_rows', 'new_clients', 'existing_clients')):
                counts.update({
                    'total_rows': int(meta.get('total_rows') or 0),
                    'new_rows': int(meta.get('new_clients') or 0),
                    'updated_rows': int(meta.get('existing_clients') or 0),
                })
            else:
                # Fallback only for old metadata versions.
                p = os.path.join(target_base, str(meta.get('storage_filename') or meta.get('filename') or 'client_db.xlsx'))
                if os.path.exists(p):
                    try:
                        ext = os.path.splitext(p)[1].lower()
                        if ext in ['.xls', '.xlsx']:
                            df = pd.read_excel(p, dtype=str)
                        else:
                            df = pd.read_csv(p, dtype=str)
                        df.fillna('', inplace=True)
                        counts['total_rows'] = len(df)
                    except Exception:
                        log.exception("Failed to count client_db rows")

            return jsonify(exists=True, filename=meta.get('storage_filename') or meta.get('filename'),
                           uploaded_at=meta.get('uploaded_at'),
                           **counts), 200

        # fallback: if any client_db.* exists but no meta file
        for existing in os.listdir(target_base):
            if existing.startswith('client_db') and os.path.splitext(existing)[1].lower() in ALLOWED_CLIENT_EXT:
                p = os.path.join(target_base, existing)
                try:
                    mtime = _dt.utcfromtimestamp(os.path.getmtime(p)).replace(microsecond=0).isoformat() + 'Z'
                    counts.update({'total_rows': 0, 'new_rows': 0, 'updated_rows': 0})
                    return jsonify(exists=True, filename=existing, uploaded_at=mtime, **counts), 200
                except Exception:
                    continue

        return jsonify(exists=False, filename=None, uploaded_at=None, **counts), 200
    except Exception:
        log.exception("Failed to read client_db info")
        return jsonify(exists=False, filename=None, uploaded_at=None, total_rows=0, new_rows=0, updated_rows=0), 500
# ---------- end client_db helpers + routes ----------

# credentials CRUD (unchanged behaviour) but pass active credential to template
@app.route("/credentials", methods=["GET", "POST"])
def credentials():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        user = request.form.get("user", "").strip()
        key = request.form.get("key", "").strip()
        env = MYDATA_ENV
        vat = request.form.get("vat", "").strip()

        # Νέα πεδία
        book_category = request.form.get("book_category", "Β").strip() or "Β"
        fpa_applicable = True if request.form.get("fpa_applicable") in ("on", "true", "1") else False
        expense_tags = request.form.getlist("expense_tags") or []
        apodeixakia_type = request.form.get("apodeixakia_type", "").strip()   # "afm" or "supplier"
        apodeixakia_supplier = request.form.get("apodeixakia_supplier", "").strip()
        apodeixakia_other_raw = request.form.get("apodeixakia_other_expenses")
        apodeixakia_other_expenses = True if apodeixakia_other_raw in ("on", "true", "1", "yes") else False
        series_settings = _series_settings_from_form(request.form)

        # Normalize apodeixakia fields: if category not selected, force-reset them
        if "αποδειξακια" not in expense_tags:
            apodeixakia_type = ""
            apodeixakia_supplier = ""
            apodeixakia_other_expenses = False

        if not name:
            flash("Απαιτείται όνομα", "error")
        else:
            entry = {
                "name": name,
                "user": user,
                "key": key,
                "env": env,
                "vat": vat,
                # προσθήκη νέων πεδίων
                "book_category": book_category,
                "fpa_applicable": fpa_applicable,
                "expense_tags": expense_tags,
                "apodeixakia_type": apodeixakia_type,
                "apodeixakia_supplier": apodeixakia_supplier,
                "apodeixakia_other_expenses": apodeixakia_other_expenses,
                "series_settings": series_settings
            }
            ok, err = add_credential(entry)
            if ok:
                # Log credential addition
                try:
                    from auth import _append_group_log, get_active_group
                    grp = get_active_group()
                    if grp:
                        _append_group_log(grp, f"Credential '{name}' added by {current_user.username if getattr(current_user, 'is_authenticated', False) else 'anonymous'}")
                except Exception:
                    pass
                flash("Αποθηκεύτηκε", "success")
            else:
                flash(err or "Αδυναμία αποθήκευσης", "error")
        return redirect(url_for("credentials"))

    # GET: φορτώνουμε τα credentials και αφήνουμε το context_processor να περάσει το active credential/ΑΦΜ
    creds = load_credentials()
    is_group_admin = False
    other_creds = []
    try:
        from auth import get_active_group
        grp = get_active_group()
        if grp and getattr(current_user, 'is_authenticated', False):
            is_group_admin = current_user.role_for_group(grp) == 'admin'
    except Exception:
        is_group_admin = False

    if is_group_admin:
        other_creds = [
            {"name": c.get("name", ""), "vat": c.get("vat", "")}
            for c in creds
            if c.get("name")
        ]

    return safe_render(
        "credentials_list.html",
        credentials=creds,
        other_creds=other_creds,
        is_group_admin=is_group_admin,
        active_page="credentials"
    )


@app.route("/credentials/edit/<name>", methods=["GET", "POST"])
def credentials_edit(name):
    creds = load_credentials()
    credential = next((c for c in creds if c.get("name") == name), None)
    if not credential:
        flash("Το credential δεν βρέθηκε", "error")
        return redirect(url_for("credentials"))

    can_edit_credential = False
    try:
        from auth import get_active_group
        grp = get_active_group()
        if grp and getattr(current_user, 'is_authenticated', False):
            can_edit_credential = current_user.role_for_group(grp) == 'admin'
    except Exception:
        can_edit_credential = False

    if request.method == "POST":
        if not can_edit_credential:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return jsonify({'ok': False, 'error': 'admin privileges required'}), 403
            flash("Η επεξεργασία credential επιτρέπεται μόνο σε διαχειριστές της ομάδας.", "error")
            return redirect(url_for("credentials"))
        new_name = (request.form.get("name") or "").strip()
        user = (request.form.get("user") or "").strip()
        key = (request.form.get("key") or "").strip()
        env = (request.form.get("env") or MYDATA_ENV).strip()
        vat = (request.form.get("vat") or "").strip()

        # Νέα πεδία από τη φόρμα επεξεργασίας
        book_category = request.form.get("book_category", "Β").strip() or "Β"
        fpa_applicable = True if request.form.get("fpa_applicable") in ("on", "true", "1") else False
        expense_tags = request.form.getlist("expense_tags") or []

        # ----- Apodeixakia fields (robust handling) -----
        apodeixakia_enabled = "αποδειξακια" in expense_tags

        if apodeixakia_enabled:
            apodeixakia_type = (request.form.get("apodeixakia_type") or "").strip()
            apodeixakia_supplier = (request.form.get("apodeixakia_supplier") or "").strip()
            apodeixakia_other_expenses = (request.form.get("apodeixakia_other_expenses") in ("on", "true", "1", "yes"))
        else:
            # If category is not selected, force-reset related fields so stale values don't stick
            apodeixakia_type = ""
            apodeixakia_supplier = ""
            apodeixakia_other_expenses = False

        series_settings = _series_settings_from_form(request.form)


        if not new_name:
            flash("Απαιτείται όνομα", "error")
            return redirect(url_for("credentials_edit", name=name))

        if new_name != name and any(c.get("name") == new_name for c in creds):
            flash("Υπάρχει ήδη άλλο credential με αυτό το όνομα", "error")
            return redirect(url_for("credentials_edit", name=name))

        new_entry = {
            "name": new_name,
            "user": user,
            "key": key,
            "env": env,
            "vat": vat,
            # Αποθηκεύουμε επίσης τα νέα πεδία
            "book_category": book_category,
            "fpa_applicable": fpa_applicable,
            "expense_tags": expense_tags,
            # Αποθηκεύουμε και τα apodeixakia πεδία
            "apodeixakia_type": apodeixakia_type,
            "apodeixakia_supplier": apodeixakia_supplier,
            "apodeixakia_other_expenses": bool(apodeixakia_other_expenses),
            "custom_categories": credential.get("custom_categories", []),
            "series_settings": series_settings or credential.get("series_settings", {})
        }

        updated = False
        for i, c in enumerate(creds):
            if c.get("name") == name:
                creds[i] = new_entry
                updated = True
                break
        if not updated:
            creds.append(new_entry)

        save_credentials(creds)

        # Όταν ο χρήστης αποθηκεύει την επεξεργασία, ορίζουμε το credential ως ενεργό
        session["active_credential"] = new_name
        flash(f"Το credential '{new_name}' αποθηκεύτηκε και ορίστηκε ως ενεργό", "success")

        return redirect(url_for("credentials"))

    # GET -> εμφανίζουμε τη φόρμα επεξεργασίας
    # Προσθέτουμε flash πληροφορία (παραμένει ως έχει)
    flash(f"Επεξεργασία credential: {credential.get('name')}", "info")

    # Ελέγχουμε αν ο χρήστης είναι admin της ενεργής ομάδας
    _is_group_admin = can_edit_credential

    # Λίστα άλλων credentials (για copy-from feature) — μόνο για admins
    other_creds = []
    if _is_group_admin:
        other_creds = [
            {"name": c.get("name", ""), "vat": c.get("vat", "")}
            for c in creds
            if c.get("name") != name
        ]

    return safe_render(
        "credentials_edit.html",
        credential=credential,
        other_creds=other_creds,
        is_group_admin=_is_group_admin,
        active_page="credentials"
    )


@app.get("/api/credentials/copy_params/<path:source_name>")
@login_required
def credentials_copy_params(source_name):
    """Return configuration params from another credential — only for group admins."""
    try:
        from auth import get_active_group
        grp = get_active_group()
        if not grp:
            return jsonify({'ok': False, 'error': 'no active group'}), 403
        if current_user.role_for_group(grp) != 'admin':
            return jsonify({'ok': False, 'error': 'admin privileges required'}), 403
    except Exception:
        return jsonify({'ok': False, 'error': 'permission check failed'}), 500

    creds = load_credentials()
    src = next((c for c in creds if c.get('name') == source_name), None)
    if not src:
        return jsonify({'ok': False, 'error': 'credential not found'}), 404

    # Return only configuration fields — never user/key/env/vat (sensitive / unique)
    COPY_FIELDS = [
        'book_category', 'fpa_applicable', 'expense_tags',
        'apodeixakia_type', 'apodeixakia_supplier', 'apodeixakia_other_expenses',
        'series_settings', 'custom_categories',
    ]
    params = {k: src.get(k) for k in COPY_FIELDS}
    return jsonify({'ok': True, 'params': params})


@app.route("/credentials/delete/<name>", methods=["POST"])
def credentials_delete(name):
    credential, was_active, cleanup = _delete_credential_and_related_data(name)

    if not credential:
        # Αν είναι AJAX, επιστρέφουμε JSON
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'error': 'not found'}), 404
        flash(f"Το credential '{name}' δεν βρέθηκε", "error")
        return redirect(url_for("credentials"))

    # Log credential deletion
    try:
        from auth import _append_group_log, get_active_group
        grp = get_active_group()
        if grp:
            _append_group_log(grp, f"Credential '{name}' deleted by {current_user.username if getattr(current_user, 'is_authenticated', False) else 'anonymous'}")
    except Exception:
        pass

    # Αν request από AJAX, επιστρέφουμε JSON ώστε το frontend fetch να το χειριστεί
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'status': 'ok',
            'deleted': name,
            'was_active': was_active,
            'data_files_removed': cleanup,
        })

    # Διαφορετικά κάνουμε τα flash + redirect όπως παλιά
    suffix = ''
    removed_count = cleanup.get('count') or 0
    failed_count = len(cleanup.get('failed') or [])
    if removed_count:
        suffix = f" Αφαιρέθηκαν {removed_count} αρχεία δεδομένων πελάτη."

    if was_active:
        flash(f"Το ενεργό credential '{name}' διαγράφηκε και αφαιρέθηκε από τα ενεργά.{suffix}", "success")
    else:
        flash(f"Το credential '{name}' διαγράφηκε.{suffix}", "success")

    if failed_count:
        flash(f"Δεν ήταν δυνατή η διαγραφή {failed_count} αρχείων. Έλεγξε τα δικαιώματα πρόσβασης.", "error")

    return redirect(url_for("credentials"))


# New route: set active credential
@app.route("/set_active", methods=["POST"])
def set_active_credential():
    wants_json = (
        request.headers.get("X-Requested-With") in {"XMLHttpRequest", "partial-nav"}
        or "application/json" in (request.headers.get("Accept") or "")
    )
    name = request.form.get("active_name")
    if not name:
        msg = "Δεν έχει επιλεγεί credential"
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 400
        flash(msg, "error")
    else:
        cred = get_cred_by_name(name)
        if not cred:
            msg = "Το credential δεν βρέθηκε"
            if wants_json:
                return jsonify({"ok": False, "error": msg}), 404
            flash(msg, "error")
        else:
            session["active_credential"] = name
            msg = f"Το ενεργό credential ορίστηκε σε {name}"
            if wants_json:
                return jsonify({
                    "ok": True,
                    "message": msg,
                    "active_name": name,
                    "vat": str(cred.get("vat") or "").strip(),
                }), 200
            flash(msg, "success")
    return redirect(url_for("credentials"))


# ---------------- Fetch page ----------------
# ---------------- Helpers for per-customer JSON & summary ----------------
def _is_receipt_record(r: dict) -> bool:
    # extracted helper used by both append and pruning logic
    if not isinstance(r, dict):
        return False
    if r.get("_is_receipt") or r.get("is_receipt"):
        return True
    t = str(r.get("type") or "").strip()
    if t.startswith("8"):
        return True
    return False


def _is_legacy_fetch_mode_enabled() -> bool:
    """Return True when legacy fetch mode is explicitly enabled.

    Priority:
    1) `LEGACY_FETCH_MODE` env var (supports 1/true/yes/on and 0/false/no/off)
    2) settings.json `legacy_fetch_mode`
    """
    def _to_bool(v, default=False):
        if isinstance(v, bool):
            return v
        if v is None:
            return default
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return default

    env_val = os.getenv("LEGACY_FETCH_MODE")
    if env_val is not None:
        return _to_bool(env_val, default=False)

    try:
        settings = load_settings() or {}
        return _to_bool(settings.get("legacy_fetch_mode"), default=False)
    except Exception:
        return False


def _doc_identity_key(doc: dict, ignore_keys=None) -> str:
    """Return a stable identity string for a document.

    This is used to dedupe/merge records while allowing fields like
    `updated_at` to differ without treating the record as a new/changed
    document.
    """
    if not isinstance(doc, dict):
        return json.dumps(doc, sort_keys=True, ensure_ascii=False)
    if ignore_keys is None:
        ignore_keys = {"updated_at"}
    else:
        ignore_keys = set(ignore_keys) | {"updated_at"}
    clean = {k: v for k, v in doc.items() if k not in ignore_keys}
    return json.dumps(clean, sort_keys=True, ensure_ascii=False)


def append_doc_to_customer_file(doc, vat):
    """
    Add or update a detailed document in the per-customer JSON file.

    Behavior differs depending on whether ``doc`` represents an invoice or a
    receipt.  Only invoices participate in the mark-based dedup/merge logic;
    receipts are treated as distinct records and will be appended even if an
    entry with the same MARK already exists (to preserve the full audit
    trail).

    Identification of a receipt is best-effort and uses any available flag
    produced by the fetch machinery (``_is_receipt`` or ``is_receipt``) or
    a ``type`` code beginning with ``8``.  This mirrors the same heuristic
    used elsewhere in the application.

    The implementation now behaves fully like CRUD:
      * **Create** when a brand-new mark appears.
      * **Update** existing entry when the same mark is fetched again (any
        field change, or promotion to χαρακτηρισμενο), merging new values
        and stamping ``updated_at`` on promotions.
      * **Delete** stray duplicates automatically – if the cache already
        contains multiple rows for the same invoice mark (perhaps due to a
        previous bug), only one record is kept and extras are removed.

    Receipts still bypass the mark-based merge and continue to append
    duplicates only when the entire payload differs.

    Filename (per group): <group_path>/{VAT}_invoices.json
    """
    if not vat:
        return False

    customer_file = get_customer_docs_file(vat)
    cache = json_read(customer_file)

    mark = str(doc.get("mark", "")).strip()
    vat_cat = str(doc.get("vatCategory", "")).strip()
    is_receipt = _is_receipt_record(doc)

    # check for legacy mode, where we simply append new documents (avoiding
    # merges and pruning entirely).  Duplicate payloads are still suppressed.
    legacy = _is_legacy_fetch_mode_enabled()

    if legacy:
        sig = json.dumps(doc, sort_keys=True, ensure_ascii=False)
        for d in cache:
            try:
                if json.dumps(d, sort_keys=True, ensure_ascii=False) == sig:
                    return False
            except Exception:
                if str(d) == str(doc):
                    return False
        cache.append(doc)
        json_write(customer_file, cache)
        return True

    # normal non-legacy behaviour follows
    if mark and not is_receipt:
        # gather indices of every entry with this mark and vatCategory
        matching_idxs = []
        for idx, existing in enumerate(cache):
            try:
                emark = str(existing.get("mark", "")).strip()
                evat = str(existing.get("vatCategory", "")).strip()
            except Exception:
                emark = ""
                evat = ""
            if emark and emark == mark and evat == vat_cat:
                matching_idxs.append(idx)

        if matching_idxs:
            first_idx = matching_idxs[0]
            existing = cache[first_idx]
            old_class = str(existing.get("classification", "")).strip()
            new_class = str(doc.get("classification", "")).strip()

            try:
                existing_sig = _doc_identity_key(existing)
                new_sig = _doc_identity_key(doc)
                same_payload = existing_sig == new_sig
            except Exception:
                same_payload = False

            # always prune duplicates for this mark, even if payload hasn't changed
            if len(matching_idxs) > 1 and same_payload and old_class == new_class:
                for j in reversed(matching_idxs[1:]):
                    cache.pop(j)
                json_write(customer_file, cache)
                return True

            # need update if payload changed, classification changed, or
            # there are stray duplicates to remove
            if not same_payload or old_class != new_class or len(matching_idxs) > 1:
                merged = dict(existing)
                merged.update(doc)
                if new_class == "χαρακτηρισμενο" and old_class != new_class:
                    merged["updated_at"] = datetime.datetime.utcnow().isoformat()
                cache[first_idx] = merged
                # drop any extras we found previously
                for j in reversed(matching_idxs[1:]):
                    cache.pop(j)
                json_write(customer_file, cache)
                try:
                    log.info("append_doc_to_customer_file: merged/updated mark=%s vat=%s", mark, vat)
                except Exception:
                    pass
                return True
            return False
    # otherwise (receipt or no existing invoice) do simple duplicate check and append
    sig = _doc_identity_key(doc)
    for d in cache:
        try:
            if _doc_identity_key(d) == sig:
                return False
        except Exception:
            if str(d) == str(doc):
                return False
    cache.append(doc)
    json_write(customer_file, cache)
    return True


# ------------ helper for pruning stale invoice records -------------------
def _date_in_range(date_str: str, from_str: str, to_str: str) -> bool:
    """Return True if *date_str* lies between *from_str* and *to_str*.

    Dates are normalized with ``normalize_input_date_to_iso`` (dd/mm/YYYY
    ↦ ISO) and compared as ISO strings.
    """
    if not date_str or not from_str or not to_str:
        return False
    def _to_iso(s: str) -> str | None:
        iso = normalize_input_date_to_iso(s)
        return iso
    d = _to_iso(date_str)
    if not d:
        return False
    f = _to_iso(from_str)
    t = _to_iso(to_str)
    if not f or not t:
        return False
    return f <= d <= t


def prune_customer_invoices(vat: str, keep_marks: set, 
                             date_from: str = None, date_to: str = None) -> bool:
    """
    Remove any **invoice** records from the per-customer JSON file whose
    mark is *not* present in ``keep_marks`` **and** whose issueDate falls
    within the optional ``date_from``/``date_to`` window.  Invoices outside
    that window are preserved.  Receipts are never deleted.

    ``date_from`` and ``date_to`` should be strings in dd/mm/YYYY (the
    same format produced by the fetch UI) or ISO; if either is missing the
    function falls back to pruning all non-kept marks (legacy behaviour).

    Returns True if the file was modified, False otherwise.
    """
    # we need a vat; if both keep_marks and date range are empty there's
    # nothing sensible to prune, so bail out early.  but an empty keep_marks
    # is acceptable when a date window is supplied.
    if not vat:
        return False
    if not keep_marks and not date_from and not date_to:
        return False
    customer_file = get_customer_docs_file(vat)
    cache = json_read(customer_file)
    new_cache = []
    changed = False
    for rec in cache:
        mark = str(rec.get("mark", "")).strip()
        if mark and mark not in keep_marks and not _is_receipt_record(rec):
            # only remove if invoice lies inside the supplied date window
            if date_from and date_to:
                if _date_in_range(rec.get("issueDate", ""), date_from, date_to):
                    changed = True
                    continue
            else:
                changed = True
                continue
        new_cache.append(rec)
    if changed:
        json_write(customer_file, new_cache)
    return changed


def prune_customer_summaries(vat: str, keep_marks: set, 
                             date_from: str = None, date_to: str = None) -> bool:
    """Remove stale invoice summaries from the per-customer summary JSON.

    Works similarly to :func:`prune_customer_invoices` but operates on summary
    records. Receipts are never deleted.
    """
    if not vat:
        return False
    if not keep_marks and not date_from and not date_to:
        return False
    summary_file = get_customer_summary_file(vat)
    summaries = json_read(summary_file)
    new_summaries = []
    changed = False
    for rec in summaries:
        mark = str(rec.get("mark", "")).strip()
        if mark and mark not in keep_marks and not _is_receipt_record(rec):
            if date_from and date_to:
                if _date_in_range(rec.get("issueDate", ""), date_from, date_to):
                    changed = True
                    continue
            else:
                changed = True
                continue
        new_summaries.append(rec)
    if changed:
        json_write(summary_file, new_summaries)
    return changed


def append_summary_to_customer_file(summary, vat):
    """
    Save summary for a customer into per-customer summary JSON
    Filename: data/{VAT}_summary.json

    Matching behaviour mirrors ``append_doc_to_customer_file``: invoices are
    deduped/merged by MARK, while receipts are always appended.  Promotion
    from unclassified to χαρακτηρισμενο is recorded with ``updated_at``.
    The old fallback-identity logic for receipts using
    ``RECEIPT_FALLBACK_MARK`` is preserved.

    When ``legacy_fetch_mode`` setting is true the function reverts to the
    simplest append-only behaviour used in earlier revisions: no merges,
    no updated_at stamps, just avoid exact duplicates.
    """
    if not vat:
        return False
    summary_file = group_path(f"{vat}_summary.json")
    summaries = json_read(summary_file)
    mark = str(summary.get("mark", "")).strip()

    # check legacy flag from settings or environment
    legacy = _is_legacy_fetch_mode_enabled()

    if legacy:
        sig = json.dumps(summary, sort_keys=True, ensure_ascii=False)
        for s in summaries:
            try:
                if json.dumps(s, sort_keys=True, ensure_ascii=False) == sig:
                    return False
            except Exception:
                if str(s) == str(summary):
                    return False
        summaries.append(summary)
        json_write(summary_file, summaries)
        return True

    def _is_receipt_summary(s: dict) -> bool:
        if not isinstance(s, dict):
            return False
        if s.get("is_receipt") or s.get("_is_receipt"):
            return True
        t = str(s.get("type") or "").strip()
        if t.startswith("8"):
            return True
        return False

    is_receipt = _is_receipt_summary(summary)

    # If this looks like a receipt *with a real mark*, treat it as an invoice
    # for merge/dedupe purposes so we don't end up with duplicate entries for
    # the same mark.
    if is_receipt and mark and mark != RECEIPT_FALLBACK_MARK:
        is_receipt = False

    # receipts (fallback mark or explicit receipts) should be deduped/merged by identity (AA + issueDate + AFM)
    # to avoid duplicate identical receipt entries while keeping audit history.
    if is_receipt:
        for idx, s in enumerate(summaries):
            if not _is_receipt_summary(s):
                continue
            if not _same_receipt_identity(summary, s):
                continue

            old_class = str(s.get("classification", "")).strip()
            new_class = str(summary.get("classification", "")).strip()
            try:
                same_payload = _doc_identity_key(s) == _doc_identity_key(summary)
            except Exception:
                same_payload = False

            if same_payload and old_class == new_class:
                return False

            # merge and possibly stamp updated_at
            merged = dict(s)
            merged.update(summary)
            if new_class == "χαρακτηρισμενο" and old_class != new_class:
                merged["updated_at"] = datetime.datetime.utcnow().isoformat()
            summaries[idx] = merged
            json_write(summary_file, summaries)
            try:
                log.info("append_summary_to_customer_file: merged/updated mark=%s vat=%s", mark, vat)
            except Exception:
                pass
            return True

        # no matching receipt found -> append
        summaries.append(summary)
        json_write(summary_file, summaries)
        return True

    # invoices are deduped/merged by mark
    matching_idxs = []
    for idx, s in enumerate(summaries):
        try:
            if str(s.get("mark", "")).strip() == mark:
                matching_idxs.append(idx)
        except Exception:
            continue

    if matching_idxs:
        first_idx = matching_idxs[0]
        existing = summaries[first_idx]
        old_class = str(existing.get("classification", "")).strip()
        new_class = str(summary.get("classification", "")).strip()

        try:
            existing_sig = _doc_identity_key(existing)
            new_sig = _doc_identity_key(summary)
            same_payload = existing_sig == new_sig
        except Exception:
            same_payload = False

        # always prune duplicates for this mark, even if nothing else changed
        if len(matching_idxs) > 1 and same_payload and old_class == new_class:
            for j in reversed(matching_idxs[1:]):
                summaries.pop(j)
            json_write(summary_file, summaries)
            return True

        if not same_payload or old_class != new_class or len(matching_idxs) > 1:
            merged = dict(existing)
            merged.update(summary)
            if new_class == "χαρακτηρισμενο" and old_class != new_class:
                merged["updated_at"] = datetime.datetime.utcnow().isoformat()
            summaries[first_idx] = merged
            for j in reversed(matching_idxs[1:]):
                summaries.pop(j)
            json_write(summary_file, summaries)
            try:
                log.info("append_summary_to_customer_file: merged/updated mark=%s vat=%s", mark, vat)
            except Exception:
                pass
            return True
        return False

    # no matching mark -> append normally
    summaries.append(summary)
    json_write(summary_file, summaries)
    return True


RECEIPT_FALLBACK_MARK = "500000000000000"


def _norm_id_value(v: Any) -> str:
    try:
        return str(v or "").strip()
    except Exception:
        return ""


def _same_receipt_identity(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """
    Robust identity check for receipts when MARK is shared fallback.
    Requires AA + issueDate + issuer AFM to all match.
    """
    try:
        a_aa = _norm_id_value(_first(a.get("number"), a.get("AA"), a.get("aa"), a.get("progressive_aa")))
        b_aa = _norm_id_value(_first(b.get("number"), b.get("AA"), b.get("aa"), b.get("progressive_aa")))
        a_date = _norm_id_value(_first(a.get("issueDate"), a.get("issue_date"), a.get("date")))
        b_date = _norm_id_value(_first(b.get("issueDate"), b.get("issue_date"), b.get("date")))
        a_afm = _norm_id_value(_first(a.get("AFM_issuer"), a.get("AFM"), a.get("issuer_vat"), a.get("issuer_afm")))
        b_afm = _norm_id_value(_first(b.get("AFM_issuer"), b.get("AFM"), b.get("issuer_vat"), b.get("issuer_afm")))
        return bool(a_aa and b_aa and a_date and b_date and a_afm and b_afm and a_aa == b_aa and a_date == b_date and a_afm == b_afm)
    except Exception:
        return False

def get_customer_summary_file(vat):
    return group_path(f"{vat}_summary.json")

def get_customer_docs_file(vat):
    return group_path(f"{vat}_invoices.json")



# ---------------- Notifications (broadcast) ----------------
# simple in-memory list of messages; cleared as they are fetched by clients.
global_notifications = []
fetch_progress_lock = threading.Lock()
fetch_progress_state = {}


def _set_fetch_progress_state(fetch_key: str, status: str, percent: int, message: str, **extra) -> None:
    """Store in-memory progress for a running fetch job."""
    key = str(fetch_key or "").strip()
    if not key:
        return

    pct = max(0, min(100, int(percent or 0)))
    payload = {
        "status": str(status or "not_started"),
        "percent": pct,
        "message": str(message or ""),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    payload.update(extra or {})

    with fetch_progress_lock:
        prev = fetch_progress_state.get(key, {})
        if "started_at" in prev and "started_at" not in payload:
            payload["started_at"] = prev.get("started_at")
        fetch_progress_state[key] = payload


def _get_fetch_progress_state(fetch_key: str) -> Dict[str, Any]:
    key = str(fetch_key or "").strip()
    if not key:
        return {"status": "not_started", "percent": 0, "message": ""}
    with fetch_progress_lock:
        current = fetch_progress_state.get(key)
        if not current:
            return {"status": "not_started", "percent": 0, "message": ""}
        return dict(current)


bulk_fetch_progress_lock = threading.Lock()
bulk_fetch_progress_state: Dict[str, Dict[str, Any]] = {}


def _set_bulk_fetch_progress(job_id: str, status: str, percent: int, message: str, **extra) -> None:
    key = str(job_id or "").strip()
    if not key:
        return
    pct = max(0, min(100, int(percent or 0)))
    payload: Dict[str, Any] = {
        "job_id": key,
        "status": str(status or "not_started"),
        "percent": pct,
        "message": str(message or ""),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    payload.update(extra or {})
    with bulk_fetch_progress_lock:
        prev = bulk_fetch_progress_state.get(key, {})
        if "started_at" in prev and "started_at" not in payload:
            payload["started_at"] = prev.get("started_at")
        bulk_fetch_progress_state[key] = payload


def _get_bulk_fetch_progress(job_id: str) -> Dict[str, Any]:
    key = str(job_id or "").strip()
    if not key:
        return {"status": "not_started", "percent": 0, "message": ""}
    with bulk_fetch_progress_lock:
        cur = bulk_fetch_progress_state.get(key)
        if not cur:
            return {"status": "not_started", "percent": 0, "message": ""}
        return dict(cur)


def _is_bulk_fetch_stop_requested(job_id: str) -> bool:
    key = str(job_id or "").strip()
    if not key:
        return False
    with bulk_fetch_progress_lock:
        cur = bulk_fetch_progress_state.get(key) or {}
        return bool(cur.get('stop_requested', False))


def _request_bulk_fetch_stop(job_id: str) -> bool:
    key = str(job_id or "").strip()
    if not key:
        return False
    with bulk_fetch_progress_lock:
        cur = dict(bulk_fetch_progress_state.get(key) or {})
        if not cur:
            return False
        cur['stop_requested'] = True
        if str(cur.get('status') or '') == 'running':
            cur['status'] = 'stopping'
            cur['message'] = 'Ζητήθηκε διακοπή. Θα ολοκληρωθεί ο τρέχων πελάτης και δεν θα ξεκινήσει επόμενος.'
        cur['updated_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        bulk_fetch_progress_state[key] = cur
        return True


def _is_active_group_admin_user() -> bool:
    try:
        from flask_login import current_user
        from auth import get_active_group

        if not getattr(current_user, 'is_authenticated', False):
            return False

        # Global admins can access admin bulk actions even if the currently
        # active group role is not resolved yet during a partial reload.
        if bool(getattr(current_user, 'is_admin', False)):
            return True

        grp = get_active_group()
        if not grp:
            return False
        return current_user.role_for_group(grp) == 'admin'
    except Exception:
        return False


@app.route('/api/fetch_bulk/start', methods=['POST'])
@login_required
def api_fetch_bulk_start():
    if not _is_active_group_admin_user():
        return jsonify({'ok': False, 'error': 'Απαιτούνται δικαιώματα admin της ενεργής ομάδας.'}), 403

    payload = request.get_json(silent=True) or {}
    date_from_raw = str(payload.get('date_from') or '').strip()
    date_to_raw = str(payload.get('date_to') or '').strip()
    date_from_iso = normalize_input_date_to_iso(date_from_raw)
    date_to_iso = normalize_input_date_to_iso(date_to_raw)
    if not date_from_iso or not date_to_iso:
        return jsonify({'ok': False, 'error': 'Παρακαλώ συμπλήρωσε έγκυρες ημερομηνίες (dd/mm/YYYY).'}), 400

    selected_names = payload.get('credential_names') or []
    if not isinstance(selected_names, list):
        selected_names = []
    selected_names = [str(x or '').strip() for x in selected_names if str(x or '').strip()]
    fetch_all = bool(payload.get('all_customers', False))

    creds = load_credentials() or []
    if fetch_all:
        targets = [c for c in creds if isinstance(c, dict) and str(c.get('name') or '').strip()]
    else:
        wanted = set(selected_names)
        targets = [c for c in creds if str(c.get('name') or '').strip() in wanted]

    if not targets:
        return jsonify({'ok': False, 'error': 'Δεν βρέθηκαν πελάτες για μαζική λήψη.'}), 400

    d1 = datetime.datetime.fromisoformat(date_from_iso).strftime('%d/%m/%Y')
    d2 = datetime.datetime.fromisoformat(date_to_iso).strftime('%d/%m/%Y')

    user_part = 'anon'
    group_part = 'nogroup'
    try:
        from flask_login import current_user
        from auth import get_active_group
        user_part = str(getattr(current_user, 'id', 'anon'))
        grp = get_active_group()
        group_part = str(getattr(grp, 'id', 'nogroup'))
    except Exception:
        pass
    job_id = f"bulk:{group_part}:{user_part}:{int(time.time())}:{secrets.token_hex(4)}"

    _set_bulk_fetch_progress(
        job_id,
        'running',
        1,
        f"Εκκίνηση μαζικής λήψης για {len(targets)} πελάτες.",
        started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        total_customers=len(targets),
        current_index=0,
        current_customer='',
        current_customer_progress=0,
        completed_customers=0,
        failed_customers=0,
        results=[],
        stop_requested=False,
    )

    group_dir = get_group_base_dir()
    aade_user_default = os.getenv("AADE_USER_ID", AADE_USER_ENV)
    aade_key_default = os.getenv("AADE_SUBSCRIPTION_KEY", AADE_KEY_ENV)

    log_actor_id = 'anonymous'
    log_actor_email = None
    log_actor_username = None
    log_group_name = 'system'
    log_group_obj = None
    app_obj = None
    try:
        from flask_login import current_user
        from auth import get_active_group
        app_obj = current_app._get_current_object()
        grp = get_active_group()
        log_group_obj = grp
        log_group_name = str(getattr(grp, 'name', None) or getattr(grp, 'data_folder', None) or 'system')
        if getattr(current_user, 'is_authenticated', False):
            log_actor_id = str(getattr(current_user, 'id', 'anonymous'))
            log_actor_email = getattr(current_user, 'email', None)
            log_actor_username = getattr(current_user, 'username', None)
    except Exception:
        pass

    def _bulk_worker(_job_id: str, _targets: List[Dict[str, Any]], _d1: str, _d2: str, _group_dir: str, _log_group_name: str, _log_group_obj, _log_actor_id: str, _log_actor_email: str, _log_actor_username: str, _app_obj):
        results: List[Dict[str, Any]] = []
        failed = 0
        done = 0
        total = max(1, len(_targets))

        try:
            _set_thread_group_base_dir(_group_dir)
        except Exception:
            pass

        stopped = False
        for idx, cred in enumerate(_targets, start=1):
            if _is_bulk_fetch_stop_requested(_job_id):
                stopped = True
                break
            name = str(cred.get('name') or '').strip()
            vat = str(cred.get('vat') or '').strip()
            user = str(cred.get('user') or aade_user_default or '').strip()
            key = str(cred.get('key') or aade_key_default or '').strip()

            base_pct = int(((idx - 1) / total) * 100)
            _set_bulk_fetch_progress(
                _job_id,
                'running',
                max(1, base_pct),
                f"Εκτελείται λήψη για τον πελάτη {name} ({idx}/{total}).",
                total_customers=total,
                current_index=idx,
                current_customer=name,
                current_customer_progress=1,
                completed_customers=done,
                failed_customers=failed,
                results=results,
                stop_requested=_is_bulk_fetch_stop_requested(_job_id),
            )

            try:
                if not user or not key:
                    raise RuntimeError('Λείπουν credentials AADE για τον πελάτη.')

                all_rows, summary_list = request_docs(
                    date_from=_d1,
                    date_to=_d2,
                    mark="000000000000000",
                    aade_user=user,
                    aade_key=key,
                    debug=True,
                    save_excel=False,
                )

                added_docs = 0
                added_summaries = 0
                seen_marks = set()
                total_rows = len(all_rows)
                total_summaries = len(summary_list)

                for r_idx, d in enumerate(all_rows, start=1):
                    if vat:
                        d['AFM_counterpart'] = vat
                    mk = str(d.get('mark') or '').strip()
                    if mk:
                        seen_marks.add(mk)
                    if append_doc_to_customer_file(d, vat):
                        added_docs += 1

                    if r_idx == 1 or r_idx == total_rows or r_idx % max(1, total_rows // 10) == 0:
                        customer_pct = min(70, int((r_idx / max(1, total_rows)) * 70))
                        overall_pct = int((((idx - 1) + (customer_pct / 100.0)) / total) * 100)
                        _set_bulk_fetch_progress(
                            _job_id,
                            'running',
                            max(1, overall_pct),
                            f"Πρόοδος πελάτη {name}: {customer_pct}% | Συνολική πρόοδος: {max(1, overall_pct)}%",
                            total_customers=total,
                            current_index=idx,
                            current_customer=name,
                            current_customer_progress=customer_pct,
                            completed_customers=done,
                            failed_customers=failed,
                            results=results,
                            stop_requested=_is_bulk_fetch_stop_requested(_job_id),
                        )

                for s_idx, s in enumerate(summary_list, start=1):
                    if append_summary_to_customer_file(s, vat):
                        added_summaries += 1
                    if s_idx == 1 or s_idx == total_summaries or s_idx % max(1, total_summaries // 10) == 0:
                        customer_pct = 70 + min(25, int((s_idx / max(1, total_summaries)) * 25))
                        overall_pct = int((((idx - 1) + (customer_pct / 100.0)) / total) * 100)
                        _set_bulk_fetch_progress(
                            _job_id,
                            'running',
                            max(1, overall_pct),
                            f"Πρόοδος πελάτη {name}: {customer_pct}% | Συνολική πρόοδος: {max(1, overall_pct)}%",
                            total_customers=total,
                            current_index=idx,
                            current_customer=name,
                            current_customer_progress=customer_pct,
                            completed_customers=done,
                            failed_customers=failed,
                            results=results,
                            stop_requested=_is_bulk_fetch_stop_requested(_job_id),
                        )

                if vat and seen_marks:
                    try:
                        legacy = _is_legacy_fetch_mode_enabled()
                        if not legacy:
                            prune_customer_invoices(vat, seen_marks, date_from=_d1, date_to=_d2)
                            prune_customer_summaries(vat, seen_marks, date_from=_d1, date_to=_d2)
                    except Exception:
                        pass

                set_last_fetch_date(_get_fetch_tracking_key(name, vat))

                try:
                    from utils import log_user_activity
                    activity_details = {
                        'date_from': str(_d1),
                        'date_to': str(_d2),
                        'vat': vat,
                        'added_docs': added_docs,
                        'added_summaries': added_summaries,
                        'fetched_count': len(all_rows)
                    }
                    if _app_obj is not None:
                        with _app_obj.app_context():
                            try:
                                log_user_activity(
                                    _log_actor_id,
                                    _log_group_name,
                                    'bulk_fetch_data',
                                    details=activity_details,
                                    user_email=_log_actor_email,
                                    user_username=_log_actor_username
                                )
                            except Exception:
                                pass
                            if _log_group_obj is not None:
                                try:
                                    from auth import _append_group_log
                                    entry_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
                                    _append_group_log(_log_group_obj, {
                                        'user_id': str(_log_actor_id) if _log_actor_id is not None else 'anonymous',
                                        'group': str(_log_group_name or getattr(_log_group_obj, 'name', None) or getattr(_log_group_obj, 'data_folder', None) or 'system'),
                                        'action': 'bulk_fetch_data',
                                        'details': {
                                            'user_id': str(_log_actor_id) if _log_actor_id is not None else 'anonymous',
                                            'user_email': _log_actor_email,
                                            'user_username': _log_actor_username,
                                            'group': str(_log_group_name or getattr(_log_group_obj, 'name', None) or getattr(_log_group_obj, 'data_folder', None) or 'system'),
                                            'action': 'bulk_fetch_data',
                                            'timestamp': entry_timestamp,
                                            'ip_address': None,
                                            'details': activity_details,
                                            'description': 'Μαζική Ανάκτηση Δεδομένων MyDATA'
                                        }
                                    })
                                except Exception:
                                    pass
                    else:
                        try:
                            log_user_activity(
                                _log_actor_id,
                                _log_group_name,
                                'bulk_fetch_data',
                                details=activity_details,
                                user_email=_log_actor_email,
                                user_username=_log_actor_username
                            )
                        except Exception:
                            pass
                except Exception:
                    log.exception('Failed to write bulk fetch activity log for credential=%s', name)

                done += 1
                results.append({
                    'credential': name,
                    'vat': vat,
                    'ok': True,
                    'added_docs': added_docs,
                    'added_summaries': added_summaries,
                    'fetched_count': total_rows,
                })
            except Exception as ex:
                failed += 1
                log.exception('Bulk fetch failed for credential=%s', name)
                results.append({
                    'credential': name,
                    'vat': vat,
                    'ok': False,
                    'error': str(ex),
                })

            end_pct = int((idx / total) * 100)
            _set_bulk_fetch_progress(
                _job_id,
                'running',
                max(1, min(99, end_pct)),
                f"Ολοκληρώθηκε ο πελάτης {name}. Συνολική πρόοδος: {max(1, min(99, end_pct))}%",
                total_customers=total,
                current_index=idx,
                current_customer=name,
                current_customer_progress=100,
                completed_customers=done,
                failed_customers=failed,
                results=results,
                stop_requested=_is_bulk_fetch_stop_requested(_job_id),
            )

            if _is_bulk_fetch_stop_requested(_job_id):
                stopped = True
                break
        if stopped:
            final_msg = f"Η μαζική λήψη σταμάτησε μετά τον τρέχοντα πελάτη. Επιτυχίες: {done}, Αποτυχίες: {failed}."
            _set_bulk_fetch_progress(
                _job_id,
                'stopped',
                100,
                final_msg,
                total_customers=total,
                current_index=done + failed,
                current_customer='',
                current_customer_progress=100,
                completed_customers=done,
                failed_customers=failed,
                results=results,
                stop_requested=True,
                finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
        else:
            final_msg = f"Η μαζική λήψη ολοκληρώθηκε. Επιτυχίες: {done}, Αποτυχίες: {failed}."
            _set_bulk_fetch_progress(
                _job_id,
                'completed',
                100,
                final_msg,
                total_customers=total,
                current_index=total,
                current_customer='',
                current_customer_progress=100,
                completed_customers=done,
                failed_customers=failed,
                results=results,
                stop_requested=False,
                finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )

    try:
        t = threading.Thread(
            target=_bulk_worker,
            args=(job_id, targets, d1, d2, group_dir, log_group_name, log_group_obj, log_actor_id, log_actor_email, log_actor_username, app_obj),
            daemon=True
        )
        t.start()
    except Exception:
        log.exception('Failed to start bulk fetch worker')
        _set_bulk_fetch_progress(job_id, 'error', 100, 'Αποτυχία εκκίνησης worker μαζικής λήψης.')
        return jsonify({'ok': False, 'error': 'Αποτυχία εκκίνησης worker μαζικής λήψης.'}), 500

    return jsonify({
        'ok': True,
        'message': f'Ξεκίνησε μαζική λήψη για {len(targets)} πελάτες.',
        'job_id': job_id,
        'total_customers': len(targets),
    }), 200


@app.route('/api/fetch_bulk/progress', methods=['GET'])
@login_required
def api_fetch_bulk_progress():
    if not _is_active_group_admin_user():
        return jsonify({'ok': False, 'error': 'Απαιτούνται δικαιώματα admin της ενεργής ομάδας.'}), 403
    job_id = str(request.args.get('job_id') or '').strip()
    if not job_id:
        return jsonify({'ok': False, 'error': 'Missing job_id'}), 400
    state = _get_bulk_fetch_progress(job_id)
    state['ok'] = True
    return jsonify(state), 200


@app.route('/api/fetch_bulk/stop', methods=['POST'])
@login_required
def api_fetch_bulk_stop():
    if not _is_active_group_admin_user():
        return jsonify({'ok': False, 'error': 'Απαιτούνται δικαιώματα admin της ενεργής ομάδας.'}), 403
    payload = request.get_json(silent=True) or {}
    job_id = str(payload.get('job_id') or '').strip()
    if not job_id:
        return jsonify({'ok': False, 'error': 'Missing job_id'}), 400
    if not _request_bulk_fetch_stop(job_id):
        return jsonify({'ok': False, 'error': 'Η εργασία δεν βρέθηκε ή δεν είναι ενεργή.'}), 404
    return jsonify({
        'ok': True,
        'message': 'Η διακοπή ζητήθηκε. Θα ολοκληρωθεί ο τρέχων πελάτης και δεν θα ξεκινήσει επόμενος.',
        'job_id': job_id,
    }), 200

@app.route('/api/global_notifications', methods=['GET'])
def api_global_notifications():
    """Return and clear any pending broadcast messages."""
    try:
        msgs = list(global_notifications)
        global_notifications.clear()
        return jsonify({"msgs": msgs}), 200
    except Exception:
        return jsonify({"msgs": []}), 500


@app.route('/api/fetch_progress', methods=['GET'])
def api_fetch_progress():
    """Return current fetch progress for credential/vat key."""
    try:
        credential = (request.args.get('credential') or '').strip()
        vat = (request.args.get('vat') or '').strip()
        fetch_key = _get_fetch_tracking_key(credential, vat)
        return jsonify(_get_fetch_progress_state(fetch_key)), 200
    except Exception as e:
        return jsonify({"status": "error", "percent": 0, "message": str(e)}), 500


# ---------------- Fetch page (updated with per-customer summary) ----------------

@app.route("/fetch", methods=["GET", "POST"])
def fetch():
    message = None
    error = None
    preview = []
    creds = load_credentials()
    active_cred = get_active_credential_from_session()
    active_name = active_cred.get("name") if active_cred else None
    initial_last_fetch_date = None

    try:
        initial_vat = str((active_cred or {}).get("vat") or "").strip()
        initial_key = _get_fetch_tracking_key(active_name or "", initial_vat)
        initial_last_raw = get_last_fetch_date(initial_key, only_meta=True) if initial_key else None
        if not initial_last_raw and initial_key:
            initial_last_raw = get_last_fetch_date(initial_key, only_meta=False)
        initial_last_fetch_date = _format_last_fetch_date_for_display(initial_last_raw)
    except Exception:
        initial_last_fetch_date = None

    wants_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or "application/json" in (request.headers.get("Accept") or "")
    )

    if request.method == "POST":
        date_from_raw = request.form.get("date_from", "").strip()
        date_to_raw = request.form.get("date_to", "").strip()
        date_from_iso = normalize_input_date_to_iso(date_from_raw)
        date_to_iso = normalize_input_date_to_iso(date_to_raw)

        if not date_from_iso or not date_to_iso:
            error = "Παρακαλώ συμπλήρωσε έγκυρες ημερομηνίες (dd/mm/YYYY)."
            if wants_json:
                return jsonify({"ok": False, "error": error}), 400
            return safe_render("fetch.html", credentials=creds, message=message,
                               error=error, preview=preview, active_page="fetch",
                               active_credential=active_name,
                               last_fetch_date_display=initial_last_fetch_date)

        d1 = datetime.datetime.fromisoformat(date_from_iso).strftime("%d/%m/%Y")
        d2 = datetime.datetime.fromisoformat(date_to_iso).strftime("%d/%m/%Y")

        selected = request.form.get("use_credential") or session.get("active_credential") or ""
        vat = request.form.get("vat_number", "").strip()
        # read env vars fresh each request (constants were evaluated on import)
        aade_user = os.getenv("AADE_USER_ID", AADE_USER_ENV)
        aade_key = os.getenv("AADE_SUBSCRIPTION_KEY", AADE_KEY_ENV)
        if selected:
            c = next((x for x in creds if x.get("name") == selected), None)
            if c:
                aade_user = c.get("user") or aade_user
                aade_key = c.get("key") or aade_key
                vat = vat or c.get("vat", "")
                session["active_credential"] = c.get("name")

        if not aade_user or not aade_key:
            error = "Δεν υπάρχουν αποθηκευμένα credentials για την κλήση."
            if wants_json:
                return jsonify({"ok": False, "error": error}), 400
            return safe_render("fetch.html", credentials=creds, message=message,
                               error=error, preview=preview, active_page="fetch",
                               active_credential=active_name,
                               last_fetch_date_display=initial_last_fetch_date)

        # capture logging context before entering the background thread
        log_group = None
        log_group_name = None
        log_actor_id = 'anonymous'
        log_actor_email = None
        log_actor_username = None
        try:
            from auth import get_active_group
            log_group = get_active_group()
            if log_group:
                log_group_name = getattr(log_group, 'name', None) or getattr(log_group, 'data_folder', None)
        except Exception:
            log_group = None
            log_group_name = None
        try:
            if getattr(current_user, 'is_authenticated', False):
                log_actor_id = getattr(current_user, 'id', 'anonymous')
                log_actor_email = getattr(current_user, 'email', None)
                log_actor_username = getattr(current_user, 'username', None)
        except Exception:
            pass

        # perform the actual fetch+save in background so the request can
        # return immediately and avoid timeouts.
        def _do_fetch(aade_user, aade_key, vat, d1, d2, selected, group_dir, fetch_key, _log_group_name=None, _log_group_obj=None, _log_actor_id='anonymous', _log_actor_email=None, _log_actor_username=None):
            # store the captured group directory in thread-local storage so that
            # any subsequent calls to ``group_path``/``get_group_base_dir``
            # inside this worker use the correct folder even though the Flask
            # request context has gone away.
            added_docs = 0
            added_summaries = 0
            all_rows = []
            summary_list = []
            seen_marks = set()
            completed_ok = False

            try:
                _set_thread_group_base_dir(group_dir)
            except Exception:
                pass

            try:
                _set_fetch_progress_state(fetch_key, "running", 5, "Ξεκίνησε η διαδικασία λήψης.", started_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
                all_rows, summary_list = request_docs(
                    date_from=d1,
                    date_to=d2,
                    mark="000000000000000",
                    aade_user=aade_user,
                    aade_key=aade_key,
                    debug=True,
                    save_excel=False
                )
                total_rows = len(all_rows)
                total_summaries = len(summary_list)
                _set_fetch_progress_state(fetch_key, "running", 20, f"Έγινε λήψη {total_rows} παραστατικών. Επεξεργασία...")

                docs_step = max(1, total_rows // 25) if total_rows else 1
                for idx, d in enumerate(all_rows, start=1):
                    if vat:
                        d["AFM_counterpart"] = vat
                    if d.get("mark"):
                        seen_marks.add(str(d.get("mark")).strip())
                    if append_doc_to_customer_file(d, vat):
                        added_docs += 1

                    if idx == 1 or idx == total_rows or idx % docs_step == 0:
                        docs_progress = 20 + int((idx / max(total_rows, 1)) * 55)
                        _set_fetch_progress_state(fetch_key, "running", docs_progress, f"Επεξεργασία παραστατικών {idx}/{total_rows}.")

                sums_step = max(1, total_summaries // 20) if total_summaries else 1
                for s_idx, s in enumerate(summary_list, start=1):
                    if append_summary_to_customer_file(s, vat):
                        added_summaries += 1

                    if s_idx == 1 or s_idx == total_summaries or s_idx % sums_step == 0:
                        sum_progress = 78 + int((s_idx / max(total_summaries, 1)) * 14)
                        _set_fetch_progress_state(fetch_key, "running", sum_progress, f"Επεξεργασία summary {s_idx}/{total_summaries}.")

                if vat and seen_marks:
                    try:
                        legacy = _is_legacy_fetch_mode_enabled()
                        if not legacy:
                            _set_fetch_progress_state(fetch_key, "running", 94, "Καθαρισμός παλιών εγγραφών...")
                            prune_customer_invoices(vat, seen_marks, date_from=d1, date_to=d2)
                            prune_customer_summaries(vat, seen_marks, date_from=d1, date_to=d2)
                    except Exception:
                        pass

                if fetch_key:
                    set_last_fetch_date(fetch_key)

                try:
                    from utils import log_user_activity
                    details = {
                        'date_from': str(d1),
                        'date_to': str(d2),
                        'vat': vat,
                        'added_docs': added_docs,
                        'added_summaries': added_summaries,
                        'fetched_count': len(all_rows)
                    }
                    if _log_group_name:
                        try:
                            log_user_activity(
                                _log_actor_id,
                                _log_group_name,
                                'fetch_data',
                                details=details,
                                user_email=_log_actor_email,
                                user_username=_log_actor_username,
                            )
                        except Exception:
                            pass
                    if _log_group_obj:
                        try:
                            from auth import _append_group_log
                            resolved_group_name = str(_log_group_name or getattr(_log_group_obj, 'name', None) or getattr(_log_group_obj, 'data_folder', None) or 'system')
                            entry_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
                            _append_group_log(_log_group_obj, {
                                'user_id': str(_log_actor_id) if _log_actor_id is not None else 'anonymous',
                                'group': resolved_group_name,
                                'action': 'fetch_data',
                                'details': {
                                    'user_id': str(_log_actor_id) if _log_actor_id is not None else 'anonymous',
                                    'user_email': _log_actor_email,
                                    'user_username': _log_actor_username,
                                    'group': resolved_group_name,
                                    'action': 'fetch_data',
                                    'timestamp': entry_timestamp,
                                    'ip_address': None,
                                    'details': details,
                                    'description': 'Ανάκτηση δεδομένων MyDATA'
                                }
                            })
                        except Exception:
                            pass
                except Exception:
                    pass
                completed_ok = True
                _set_fetch_progress_state(
                    fetch_key,
                    "completed",
                    100,
                    f"Η λήψη ολοκληρώθηκε: {added_docs} έγγραφα, {added_summaries} συνοψίσεις.",
                    added_docs=added_docs,
                    added_summaries=added_summaries,
                    fetched_count=len(all_rows),
                )
            except Exception:
                log.exception("Fetch error (background)")
                _set_fetch_progress_state(fetch_key, "error", 100, "Σφάλμα κατά τη λήψη. Ελέγξτε τα logs.")
            # broadcast notification for any listening clients
            try:
                selected_name = str(selected or '').strip()
                vat_text = str(vat or '').strip()
                target_text = selected_name and vat_text and f"{selected_name} (ΑΦΜ {vat_text})" or (selected_name or (vat_text and f"ΑΦΜ {vat_text}") or "άγνωστος πελάτης")
                if completed_ok:
                    global_notifications.append(f"Λήψη ολοκληρώθηκε για {target_text}: {added_docs} έγγραφα, {added_summaries} συνοψίσεις.")
                else:
                    global_notifications.append(f"Λήψη απέτυχε για {target_text}. Δείτε τα logs.")
            except Exception:
                pass

        # spawn thread and return early.  capture the current group directory
        # so the worker can continue to write to the same location.
        fetch_key = _get_fetch_tracking_key(selected, vat)
        current_progress = _get_fetch_progress_state(fetch_key)
        if current_progress.get("status") == "running":
            error = "Υπάρχει ήδη ενεργή λήψη για αυτόν τον πελάτη."
            if wants_json:
                return jsonify({"ok": False, "error": error, "status": "running"}), 409
            return safe_render("fetch.html", credentials=creds, message=message,
                               error=error, preview=preview, active_page="fetch",
                               active_credential=active_name,
                               last_fetch_date_display=initial_last_fetch_date)

        _set_fetch_progress_state(fetch_key, "running", 1, "Η λήψη ξεκίνησε.", started_at=datetime.datetime.now(datetime.timezone.utc).isoformat())

        group_dir = get_group_base_dir()
        try:
            t = threading.Thread(
                target=_do_fetch,
                args=(
                    aade_user,
                    aade_key,
                    vat,
                    d1,
                    d2,
                    selected,
                    group_dir,
                    fetch_key,
                    log_group_name,
                    log_group,
                    log_actor_id,
                    log_actor_email,
                    log_actor_username,
                ),
                daemon=True,
            )
            t.start()
        except Exception:
            log.exception("Failed to start background fetch thread")

        message = "Fetch started – results will be saved shortly."
        preview = []

        if wants_json:
            fetch_key = _get_fetch_tracking_key(selected, vat)
            current_last_fetch_raw = get_last_fetch_date(fetch_key, only_meta=True) if fetch_key else None
            if not current_last_fetch_raw and fetch_key:
                current_last_fetch_raw = get_last_fetch_date(fetch_key, only_meta=False)
            current_last_fetch_date = _format_last_fetch_date_for_display(current_last_fetch_raw)
            return jsonify({
                "ok": True,
                "started": True,
                "message": message,
                "credential": selected,
                "vat": vat,
                "fetch_key": fetch_key,
                "last_fetch_date": current_last_fetch_date,
                "last_fetch_raw": current_last_fetch_raw,
            })

    return safe_render("fetch.html", credentials=creds, message=message,
                       error=error, preview=preview, active_page="fetch",
                       active_credential=active_name,
                       last_fetch_date_display=initial_last_fetch_date)


@app.route("/credentials/get_settings", methods=["GET"])
def credentials_get_settings():
    """
    Επιστρέφει τα stored general settings σε JSON — βολικό για AJAX αν το cog τα φορτώνει δυναμικά.
    """
    try:
        settings = load_settings() or {}
        return jsonify({"status": "ok", "settings": settings})
    except Exception as e:
        log.exception("Could not return settings")
        return jsonify({"status":"error","error":str(e)}), 500


@app.route("/api/check_mark", methods=["POST"])
def api_check_mark():
    """
    Payload JSON: { vat: str, mark: str }
    Επιστρέφει: { found: bool, source: "excel"/"none", characteristic: str|null, row: {...} }
    """
    try:
        payload = request.get_json(force=True)
        vat = payload.get("vat")
        mark = payload.get("mark")
        if not vat or not mark:
            return jsonify({"ok": False, "error": "missing vat or mark"}), 400

        safe_vat = secure_filename(vat)
        excel_path = os.path.join("uploads", f"{safe_vat}_invoices.xlsx")
        if not os.path.exists(excel_path):
            return jsonify({"ok": True, "found": False}), 200

        # διαβάζουμε το excel (single sheet)
        try:
            df = pd.read_excel(excel_path, dtype=str)  # read everything as str for safe compare
        except Exception as e:
            current_app.logger.exception("Failed to read excel for check_mark")
            return jsonify({"ok": False, "error": "cannot_read_excel", "detail": str(e)}), 500

        found, row, col = _match_row_by_mark(df, mark)
        if not found:
            return jsonify({"ok": True, "found": False}), 200

        # προσπαθούμε να βρούμε χαρακτηρισμό:
        # 1) πρώτα ψάχνουμε στο epsilon cache αν υπάρχει (καλύτερο για authoritative value)
        epsilon_path = os.path.join(group_path("epsilon"), f"{safe_vat}_epsilon_invoices.json")
        epsilon_list = _load_json(epsilon_path) or []
        # αναζητάμε στην epsilon λίστα για το ίδιο mark
        def _match_in_epsilon(item):
            for candidate_key in ("mark", "MARK", "invoice_id", "id", "Αριθμός Μητρώου", "Αριθμός"):
                if candidate_key in item and str(item[candidate_key]).strip() == str(mark).strip():
                    return True
            return False
        matched_eps = None
        for it in epsilon_list:
            if _match_in_epsilon(it):
                matched_eps = it
                break

        characteristic = None
        if matched_eps:
            # Έχουμε authoritative χαρακτηρισμό στον epsilon cache (προτεραιότητα)
            characteristic = matched_eps.get("χαρακτηρισμός") or matched_eps.get("characteristic") or matched_eps.get("flag")
        else:
            # αλλιώς ελέγχουμε αν υπάρχει στήλη χαρακτηρισμός στο excel row
            for candidate in ("χαρακτηρισμός", "χαρακτηρισμος", "characteristic", "flag", "Χαρακτηρισμός"):
                if candidate in row and row[candidate] not in (None, "", "nan"):
                    characteristic = row[candidate]
                    break

        return jsonify({
            "ok": True,
            "found": True,
            "source": "excel",
            "excel_column_used": col,
            "row": row,
            "characteristic": characteristic
        }), 200

    except Exception as e:
        current_app.logger.exception("api_check_mark failed")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/update_epsilon_characteristic", methods=["POST"])
def api_update_epsilon_characteristic():
    """
    Payload JSON: { vat: str, mark: str, characteristic: str }
    Ενημερώνει ΜΟΝΟ το data/epsilon/{vat}_epsilon_invoices.json το πεδίο χαρακτηρισμός
    (δημιουργεί εγγραφή αν δεν υπάρχει).
    """
    try:
        payload = request.get_json(force=True)
        vat = payload.get("vat")
        mark = payload.get("mark")
        new_char = payload.get("characteristic")
        if not vat or not mark:
            return jsonify({"ok": False, "error": "missing vat or mark"}), 400

        safe_vat = secure_filename(vat)
        epsilon_dir = group_path("epsilon")
        os.makedirs(epsilon_dir, exist_ok=True)
        epsilon_path = os.path.join(epsilon_dir, f"{safe_vat}_epsilon_invoices.json")
        epsilon_list = _load_json(epsilon_path) or []

        # Try to find existing invoice by common keys
        def _match(item):
            for candidate_key in ("mark", "MARK", "invoice_id", "id", "Αριθμός Μητρώου", "Αριθμός"):
                if candidate_key in item and str(item[candidate_key]).strip() == str(mark).strip():
                    return True
            return False

        found = False
        for i, item in enumerate(epsilon_list):
            if _match(item):
                # update only χαρακτηρισμός-related keys
                epsilon_list[i]["χαρακτηρισμός"] = new_char
                epsilon_list[i]["characteristic"] = new_char  # για συμβατότητα
                epsilon_list[i]["_updated_at"] = datetime.utcnow().isoformat() + "Z"
                found = True
                break

        if not found:
            # Δημιουργούμε ελάχιστη εγγραφή μέσα στην epsilon cache (χωρίς άγγιγμα Excel)
            new_item = {
                "mark": mark,
                "χαρακτηρισμός": new_char,
                "characteristic": new_char,
                "_created_at": datetime.utcnow().isoformat() + "Z"
            }
            epsilon_list.append(new_item)

        _save_json(epsilon_path, epsilon_list)
        try:
            # Log reclassification activity for live-update detection
            try:
                from utils import log_user_activity
                from flask_login import current_user
                from auth import get_active_group
                grp = get_active_group()
                log_user_activity(
                    user_id=getattr(current_user, 'id', None) or getattr(current_user, 'pw_hash', None),
                    group_name=grp.name if grp else 'unknown',
                    action='reclassify',
                    details={
                        'vat': vat,
                        'mark': mark,
                        'description': f'Επαναχαρακτηρισμός mark={mark} -> {new_char}'
                    },
                    user_email=getattr(current_user, 'email', None),
                    user_username=getattr(current_user, 'username', None)
                )
            except Exception:
                log.exception('api_update_epsilon_characteristic: failed to log activity')
        except Exception:
            pass

        return jsonify({"ok": True, "updated": True, "found_existing": found}), 200

    except Exception as e:
        current_app.logger.exception("api_update_epsilon_characteristic failed")
        return jsonify({"ok": False, "error": str(e)}), 500


# ---- simple debugging endpoint used by client JS ----
@app.route('/_debug_log', methods=['GET','POST'])
def _debug_log():
    # write incoming message to a file in workspace root for live inspection
    msg = request.args.get('msg') if request.method == 'GET' else request.form.get('msg')
    if msg is None:
        try:
            data = request.get_json(silent=True)
            msg = data.get('msg') if data else None
        except Exception:
            msg = None
    if msg is None:
        return ('', 204)
    logpath = os.path.join(os.getcwd(), 'receipt_debug.log')
    try:
        with open(logpath, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.datetime.utcnow().isoformat()} {msg}\n")
    except Exception:
        pass
    return ('', 204)

# ---------------- MARK search ----------------
@app.route("/search", methods=["GET", "POST"])
def search():
    result = None
    error = None
    mark = ""
    modal_summary = None
    invoice_lines = []
    customer_categories = []
    allow_edit_existing = False
    table_html = ""
    file_exists = False
    css_numcols = ""
    modal_warning = None
    fiscal_mismatch_block = False
    scrape_url_is_receipt = False
    classified_flag = False
    classified_message = ""
    is_ajax_search = (
        (request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest")
        or (request.args.get("ajax") == "1")
        or (request.form.get("ajax") == "1")
    )

    # active year for template (used by receipts fiscal check)
    try:
        active_year_val = get_active_fiscal_year()
    except Exception:
        active_year_val = None

    # ---------- helpers ----------
    def _map_invoice_type_local(code):
        INVOICE_TYPE_MAP = {
            "1.1": "Τιμολόγιο Πώλησης",
            "1.2": "Τιμολόγιο Πώλησης / Ενδοκοινοτικές Παραδόσεις",
            "1.3": "Τιμολόγιο Πώλησης / Παραδόσεις Τρίτων Χωρών",
            "1.4": "Τιμολόγιο Πώλησης / Πώληση για Λογαριασμό Τρίτων",
            "1.5": "Τιμολόγιο Πώλησης / Εκκαθάριση Πωλήσεων Τρίτων - Αμοιβή από Πωλήσεις Τρίτων",
            "1.6": "Τιμολόγιο Πώλησης / Συμπληρωματικό Παραστατικό",
            "2.1": "Τιμολόγιο Παροχής Υπηρεσιών",
            "2.2": "Τιμολόγιο Παροχής / Ενδοκοινοτική Παροχή Υπηρεσιών",
            "2.3": "Τιμολόγιο Παροχής / Παροχή Υπηρεσιών σε λήπτη Τρίτης Χώρας",
            "2.4": "Τιμολόγιο Παροχής / Συμπληρωματικό Παραστατικό",
            "3.1": "Τίτλος Κτήσης (μη υπόχρεος Εκδότης)",
            "3.2": "Τίτλος Κτήσης (άρνηση έκδοσης από υπόχρεο Εκδότη)",
            "5.1": "Πιστωτικό Τιμολόγιο / Συσχετιζόμενο",
            "5.2": "Πιστωτικό Τιμολόγιο / Μη Συσχετιζόμενο",
            "6.1": "Στοιχείο Αυτοπαράδοσης",
            "6.2": "Στοιχείο Ιδιοχρησιμοποίησης",
            "7.1": "Συμβόλαιο - Έσοδο",
            "8.1": "Ενοίκια - Έσοδο",
            "8.2": "Τέλος ανθεκτικότητας κλιματικής κρίσης",
            "8.4": "Απόδειξη Είσπραξης POS",
            "8.5": "Απόδειξη Επιστροφής POS",
            "8.6": "Δελτίο Παραγγελίας Εστίασης",
            "9.3": "Δελτίο Αποστολής",
            "11.1": "ΑΛΠ",
            "11.2": "ΑΠΥ",
            "11.3": "Απλοποιημένο Τιμολόγιο",
            "11.4": "Πιστωτικό Στοιχείο Λιανικής",
            "11.5": "Απόδειξη Λιανικής Πώλησης για Λογαριασμό Τρίτων",
            "13.1": "Έξοδα - Αγορές Λιανικών Συναλλαγών ημεδαπής / αλλοδαπής",
            "13.2": "Παροχή Λιανικών Συναλλαγών ημεδαπής / αλλοδαπής",
            "13.3": "Κοινόχρηστα",
            "13.4": "Συνδρομές",
            "13.30": "Παραστατικά Οντότητας ως Αναγράφονται από την ίδια (Δυναμικό)",
            "13.31": "Πιστωτικό Στοιχείο Λιανικής ημεδαπής / αλλοδαπής",
            "14.1": "Τιμολόγιο / Ενδοκοινοτικές Αποκτήσεις",
            "14.2": "Τιμολόγιο / Αποκτήσεις Τρίτων Χωρών",
            "14.3": "Τιμολόγιο / Ενδοκοινοτική Λήψη Υπηρεσιών",
            "14.4": "Τιμολόγιο / Λήψη Υπηρεσιών Τρίτων Χωρών",
            "14.5": "ΕΦΚΑ και λοιποί Ασφαλιστικοί Οργανισμοι",
            "14.30": "Παραστατικά Οντότητας ως Αναγράφονται από την ίδια (Δυναμικό)",
            "14.31": "Πιστωτικό ημεδαπής / αλλοδαπής",
            "15.1": "Συμβόλαιο - Έξοδο",
            "16.1": "Ενοίκιο Έξοδο",
            "17.1": "Μισθοδοσία",
            "17.2": "Αποσβέσεις",
            "17.3": "Λοιπές Εγγραφές Τακτοποίησης Εσόδων - Λογιστική Βάση",
            "17.4": "Λοιπές Εγγραφές Τακτοποίησης Εσόδων - Φορολογική Βάση",
            "17.5": "Λοιπές Εγγραφές Τακτοποίησης Εξόδων - Λογιστική Βάση",
            "17.6": "Λοιπές Εγγραφές Τακτοποίησης Εξόδων - Φορολογική Βάση",
        }
        return INVOICE_TYPE_MAP.get(str(code), str(code) or "")

    mapper = globals().get("map_invoice_type", None) or _map_invoice_type_local

    def float_from_comma(value):
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().replace(".", "").replace(",", ".")
        try:
            return float(s)
        except Exception:
            return 0.0

    def pick(src: dict, *keys, default=""):
        for k in keys:
            if k in src and src.get(k) not in (None, ""):
                return src.get(k)
        return default

    # ---------- credentials helpers ----------
    def credentials_path():
        # prefer per-group credentials file
        try:
            return credentials_path_for_request()
        except Exception:
            return os.path.join(DATA_DIR, "credentials.json")

    def read_credentials_list_local():
        try:
            p = credentials_path()
            return json_read(p) or []
        except Exception:
            log.exception("read_credentials_list_local failed")
            return []

    def write_credentials_list_local(creds_list):
        try:
            p = credentials_path()
            try:
                json_write(p, creds_list)
            except Exception:
                tmp = p + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(creds_list, f, ensure_ascii=False, indent=2)
                os.replace(tmp, p)
            create_excel_fn = globals().get("create_empty_excel_for_vat")
            for c in (creds_list or []):
                try:
                    if not isinstance(c, dict):
                        continue
                    vat_c = c.get("vat") or c.get("AFM") or c.get("tax_number")
                    if vat_c and create_excel_fn and callable(create_excel_fn):
                        try:
                            create_excel_fn(vat_c)
                        except Exception:
                            log.exception("write_credentials_list_local: create_empty_excel_for_vat failed for %s", vat_c)
                except Exception:
                    log.exception("write_credentials_list_local: error ensuring excel for one credential")
            return True
        except Exception:
            log.exception("write_credentials_list_local failed")
            return False

    def find_active_client_index_local(creds_list, vat_to_match=None):
        if not creds_list:
            return None
        if vat_to_match:
            for i, c in enumerate(creds_list):
                try:
                    if str(c.get("vat", "")).strip() == str(vat_to_match).strip():
                        return i
                except Exception:
                    continue
        for i, c in enumerate(creds_list):
            if c.get("active"):
                return i
        return 0

    # ---------- active credential ----------
    active_cred = get_active_credential_from_session() or {}
    vat = str(active_cred.get("vat") or "").strip() if active_cred else ""

    # testing hook: allow forcing a receipt summary via URL parameter
    if request.args.get('test_receipt_summary'):
        modal_summary = {"is_receipt": True, "lines": [{"id":"r0","amount":"","vat":"","category":""}]}

    # IMPORTANT: session payload may be partial/stale. Resolve the full credential
    # from credentials.json so custom_categories/applies_to_receipts are always present.
    try:
        creds_for_active = read_credentials_list_local() or []
    except Exception:
        creds_for_active = []

    active_cred_full = None
    if vat:
        active_cred_full = next(
            (c for c in creds_for_active if str(c.get("vat") or "").strip() == vat),
            None,
        )
    if active_cred_full is None:
        active_name = str(active_cred.get("name") or "").strip()
        if active_name:
            active_cred_full = next(
                (c for c in creds_for_active if str(c.get("name") or "").strip() == active_name),
                None,
            )
    if active_cred_full is not None:
        active_cred = active_cred_full
        vat = str(active_cred.get("vat") or vat).strip()

    # load customer_categories default from active_cred (used for invoices)
    customer_category_labels = dict(DEFAULT_INVOICE_CATEGORY_LABELS)
    receipt_custom_categories: List[str] = []
    has_receipt_custom_categories = False
    try:
        customer_categories = _list_invoice_categories(active_cred)
        # compute only custom categories that are enabled for receipts
        try:
            for item in _ensure_custom_categories_list(active_cred):
                if item and item.get("enabled") and _custom_category_receipts_enabled(item):
                    slug = str(item.get("id") or item.get("slug") or "").strip()
                    if slug and slug.lower() != "αποδειξακια":
                        receipt_custom_categories.append(slug)
            has_receipt_custom_categories = bool(receipt_custom_categories)
        except Exception:
            receipt_custom_categories = []
            has_receipt_custom_categories = False
        if not customer_categories:
            customer_categories = [
                "αγορες_εμπορευματων",
                "αγορες_α_υλων",
                "γενικες_δαπανες",
                "αμοιβες_τριτων",
                "δαπανες_χωρις_φπα"
            ]
        customer_category_labels.update(_category_labels_for_client(active_cred))
    except Exception:
        customer_categories = [
            "αγορες_εμπορευματων",
            "αγορες_α_υλων",
            "γενικες_δαπανες",
            "αμοιβες_τριτων",
            "δαπανες_χωρις_φπα"
        ]
        receipt_custom_categories = []
        has_receipt_custom_categories = False
    customer_vat_constraints = _category_vat_constraints(active_cred)
    # -------- additional check: if there are ANY saved receipt-mode
    # profiles with non-empty mappings, consider this customer as having
    # receipt-custom categories.  Profiles may encode the accounts that the
    # user filled out even if the credential itself lacks a flag.
    if not has_receipt_custom_categories and isinstance(active_cred, dict):
        for p in active_cred.get("char_profiles", []):
            if str(p.get("mode", "invoices")).lower() == "receipts":
                mapping = p.get("mapping") or p.get("map") or {}
                if any(str(v or "").strip() for v in mapping.values()):
                    has_receipt_custom_categories = True
                    break
    
    # Γ Category: Filter categories to only those with MTYPE codes
    g_category_data = None
    if active_cred:
        from g_category_helpers import is_g_category_active, get_available_categories_for_g, enrich_categories_with_mtype
        if is_g_category_active(active_cred):
            # Φορτώνουμε settings για Γ
            settings = load_settings()
            log.info(f"[Γ Category UI] Settings loaded: {bool(settings)}, categories before filter: {customer_categories}")
            if settings:
                # Φιλτράρουμε μόνο τις categories με MTYPE
                filtered_categories = get_available_categories_for_g(settings, customer_categories)
                log.info(f"[Γ Category UI] Categories after filter: {filtered_categories}")
                customer_categories = filtered_categories
                # Εμπλουτίζουμε με MTYPE info
                g_category_data = enrich_categories_with_mtype(customer_categories, settings)
                log.info(f"[Γ Category UI] G category data: {g_category_data}")
            else:
                log.warning("[Γ Category UI] No settings found - categories not filtered")

    # --- Handle JSON AJAX request to save repeat mapping ---
    if request.method == "POST" and request.is_json:
        data = request.get_json() or {}
        if data.get("action") == "save_repeat_entry":
            enabled = bool(data.get("enabled", False))
            mapping = data.get("mapping", {}) or {}
            creds = read_credentials_list_local()
            idx = find_active_client_index_local(creds, vat_to_match=vat)
            if idx is None:
                return jsonify({"ok": False, "error": "No credentials found to save."}), 400
            try:
                creds[idx]["repeat_entry"] = {"enabled": enabled, "mapping": mapping}
                ok = write_credentials_list_local(creds)
                if not ok:
                    return jsonify({"ok": False, "error": "Failed to write credentials file."}), 500
                return jsonify({"ok": True})
            except Exception as e:
                log.exception("Failed saving repeat entry mapping")
                return jsonify({"ok": False, "error": str(e)}), 500

    # emulate POST: GET ?mark=...&force_edit=1
    emulate_post = False
    if request.method == "GET" and request.args.get("mark") and request.args.get("force_edit"):
        mark = request.args.get("mark", "").strip()
        emulate_post = True

    if request.method == "POST" or emulate_post:
        if request.method == "POST" and not request.is_json:
            mark = request.form.get("mark", "").strip()

        # --- receipt vs invoice mode from form (hidden input expect_receipt=1) ---
        expect_receipt = (
            (request.form.get("expect_receipt") == "1") or
            (request.args.get("expect_receipt") == "1")
        )

        import re
        from urllib.parse import urlparse
        # existing invoice scrapers
        from scraper import scrape_wedoconnect, scrape_mydatapi, scrape_einvoice, scrape_impact, scrape_epsilon, scrape_pegcloud, scrape_einvoicing_gr, scrape_vsgr, scrape_megasoft
        # safe import of receipt scraper
        try:
            from scraper_receipt import detect_and_scrape as detect_and_scrape_receipt
        except Exception:
            detect_and_scrape_receipt = None

        # Αν ο scanner έστειλε payload τύπου "MARK https://..." ή "... https:/...",
        # πάρε πρώτα το URL token για να ακολουθήσει η ροή fetch αντί cache-by-MARK.
        embedded_url = re.search(r'https?://[^\s]+', mark, re.I)
        if embedded_url:
            mark = embedded_url.group(0).strip()

        # Ειδική ανάκτηση για malformed payloads που περιέχουν mydatapi URL χωρίς
        # καθαρό scheme token στην αρχή (π.χ. "MARK ... mydatapi.aade.gr/.../QRInfo?q=...").
        if not re.match(r'^https?://', mark, re.I):
            md = re.search(r'(mydatapi\.aade\.gr/[^\s"\'<>]*TimologioQR/QRInfo\?q=[^\s"\'<>]+)', mark, re.I)
            if md:
                mark = 'https://' + md.group(1)

        # Καθάρισε malformed διπλό scheme από scanner payloads (π.χ. https://https:/...).
        mark = re.sub(r'^(https?://)+(https?:/)', r'\2', mark, flags=re.I)
        # Ελάχιστη διόρθωση protocol μόνο για routing (https:/x -> https://x).
        mark = re.sub(r'^(https?):/([^/])', r'\1://\2', mark, flags=re.I)

        # Απέφυγε λάθος διπλό prepend όταν έχουμε ήδη "https:/...".
        input_is_url = bool(re.match(r'^https?://', mark, re.I) or re.match(r'^https?:/', mark, re.I))

        # Normalization: αν λείπει τελείως protocol, πρόσθεσε το μόνο σε καθαρά domain-like inputs.
        if (not input_is_url) and (' ' not in mark) and re.match(r'^[a-z0-9]', mark, re.I):
            # Ψάχνουμε αν μοιάζει με domain (περιέχει . ή /)
            if '.' in mark or '/' in mark:
                mark = 'https://' + mark
                input_is_url = True

        # Για URLs τύπου https:/... άφησε τη διόρθωση στο scraper.py (_normalize_url).
        if re.match(r'^https?:/', mark, re.I):
            input_is_url = True
        
        if input_is_url:
            domain = urlparse(mark).netloc.lower()

            # --- NEW: αν είναι ενεργό "Αποδείξεις" αλλά το URL είναι "τιμολογιακό", δείξε warning και ΜΗ συνεχίσεις ---
            invoice_domains = (
                "wedoconnect",
                "mydatapi.aade.gr",
                "einvoice.s1ecos.gr",
                "impact.gr",
                "einvoice.impact.gr",
                "epsilonnet.gr",
            )
            is_invoice_url = any(d in domain for d in invoice_domains)
            if expect_receipt and is_invoice_url:
                modal_warning = (
                    "Έχεις επιλεγμένη «Αποδείξεις», αλλά το URL φαίνεται να είναι Τιμολογίου. "
                    "Η εισαγωγή μπλοκάρεται — βάλε URL απόδειξης ή άλλαξε σε «Τιμολόγια»."
                )
            else:
                scraped_afm = None
                scraped_marks = []
                try:
                    if "wedoconnect" in domain:
                        scraped_marks, scraped_afm = scrape_wedoconnect(mark)
                        # Cleanup: ensure marks are valid 15-digit strings
                        if scraped_marks:
                            scraped_marks = [m.strip() for m in scraped_marks if m and len(str(m).strip()) == 15]
                    elif "mydatapi.aade.gr" in domain:
                        data = scrape_mydatapi(mark)
                        mark_val = data.get("MARK", "N/A")
                        scraped_marks = [mark_val] if mark_val != "N/A" and len(str(mark_val).strip()) == 15 else []
                        scraped_afm = data.get("ΑΦΜ Πελάτη")
                    elif "einvoice.s1ecos.gr" in domain:
                        scraped_marks, scraped_afm = scrape_einvoice(mark)
                        # Cleanup: ensure marks are valid 15-digit strings
                        if scraped_marks:
                            scraped_marks = [m.strip() for m in scraped_marks if m and len(str(m).strip()) == 15]
                    elif "einvoice.impact.gr" in domain or "impact.gr" in domain:
                        mark_val, scraped_afm_impact = scrape_impact(mark)
                        scraped_marks = [mark_val] if mark_val and len(str(mark_val).strip()) == 15 else []
                        if not scraped_afm:
                            scraped_afm = scraped_afm_impact
                    elif "epsilonnet.gr" in domain:
                        mark_val, scraped_afm_eps, _ = scrape_epsilon(mark)
                        scraped_marks = [mark_val] if mark_val and len(str(mark_val).strip()) == 15 else []
                        if not scraped_afm:
                            scraped_afm = scraped_afm_eps
                    elif "e-invoicing.pegcloud.io" in domain:
                        mark_val, scraped_afm_peg = scrape_pegcloud(mark)
                        scraped_marks = [mark_val] if mark_val and len(str(mark_val).strip()) == 15 else []
                        if not scraped_afm:
                            scraped_afm = scraped_afm_peg
                    elif "e-invoicing.gr" in domain:
                        eg_res = scrape_einvoicing_gr(mark, return_meta=True)
                        eg_meta = {}
                        if isinstance(eg_res, (tuple, list)) and len(eg_res) >= 3 and isinstance(eg_res[2], dict):
                            mark_val, scraped_afm_eg, eg_meta = eg_res[0], eg_res[1], eg_res[2]
                        elif isinstance(eg_res, (tuple, list)) and len(eg_res) >= 2:
                            mark_val, scraped_afm_eg = eg_res[0], eg_res[1]
                        else:
                            mark_val, scraped_afm_eg = None, None

                        scrape_url_is_receipt = bool((eg_meta or {}).get("is_receipt"))
                        scraped_marks = [mark_val] if mark_val and len(str(mark_val).strip()) == 15 else []
                        if not scraped_afm:
                            scraped_afm = scraped_afm_eg

                        # Invoice flow guard: e-invoicing URL αντιστοιχεί σε απόδειξη λιανικής
                        if scrape_url_is_receipt and not expect_receipt:
                            modal_warning = (
                                "Το URL αντιστοιχεί σε Απόδειξη Λιανικής, όχι σε Τιμολόγιο. "
                                "Η εισαγωγή στο flow Τιμολογίων μπλοκάρεται — άλλαξε σε «Αποδείξεις»."
                            )
                            flash("Εντοπίστηκε απόδειξη λιανικής. Χρησιμοποίησε flow «Αποδείξεις».", "warning")
                            scraped_afm = None
                            scraped_marks = []
                    elif "vs.gr" in domain:
                        scraped_marks, scraped_afm_vs = scrape_vsgr(mark)
                        # Cleanup: ensure marks are valid 15-digit strings
                        if scraped_marks:
                            scraped_marks = [m.strip() for m in scraped_marks if m and len(str(m).strip()) == 15]
                        if not scraped_afm:
                            scraped_afm = scraped_afm_vs
                    elif "megasoft" in domain or "invoicelink" in domain:
                        scraped_marks, scraped_afm_mg = scrape_megasoft(mark)
                        if scraped_marks:
                            scraped_marks = [m.strip() for m in scraped_marks if m and len(str(m).strip()) == 15]
                        if not scraped_afm:
                            scraped_afm = scraped_afm_mg
                    else:
                        # fallback try receipt detector
                        if detect_and_scrape_receipt:
                            try:
                                rd = detect_and_scrape_receipt(mark)
                                if isinstance(rd, dict) and rd.get("MARK"):
                                    scraped_marks = [str(rd.get("MARK"))]
                                    scraped_afm = rd.get("issuer_vat") or rd.get("issuer_afm")
                            except Exception:
                                log.exception("Receipt detect_and_scrape failed for URL %s", mark)
                        if not scraped_marks:
                            flash("Άγνωστο URL για scraping.", "error")
                except Exception as e:
                    log.exception("Scraping failed for URL %s", mark)
                    error = f"Αποτυχία ανάγνωσης URL: {str(e)}"

                if scraped_afm and vat and str(scraped_afm).strip() != str(vat).strip():
                    modal_warning = f"Το URL επιστρέφει ΑΦΜ {scraped_afm}, διαφορετικό από τον ενεργό πελάτη {vat}."

                if scraped_marks:
                    mark = scraped_marks[0]

        if not input_is_url:
            if not vat:
                error = "Επέλεξε πρώτα έναν πελάτη (ΑΦΜ) για αναζήτηση."
            elif not mark or not mark.isdigit() or len(mark) != 15:
                error = "Πρέπει να δώσεις έγκυρο 15ψήφιο MARK."

        if not error:
            # --- φορτώνουμε cache invoices ---
            customer_file = group_path(f"{vat}_invoices.json")
            try:
                cache = json_read(customer_file) or []
            except Exception:
                log.exception("Failed to read customer_file %s", customer_file)
                cache = []
            docs_for_mark = [d for d in cache if str(d.get("mark", "")).strip() == mark]

            # flag for already classified docs
            classified_docs = [d for d in docs_for_mark if str(d.get("classification", "")).strip().lower() == "χαρακτηρισμενο"]
            if classified_docs:
                classified_message = f"Το MARK {mark} είναι ήδη χαρακτηρισμένο στο invoices.json."
                if not is_ajax_search:
                    flash(classified_message, "warning")
                classified_flag = True

            # check duplicate in excel
            try:
                excel_path = excel_path_for(vat=vat)
                if os.path.exists(excel_path):
                    import pandas as pd
                    df_check = pd.read_excel(excel_path, engine="openpyxl", dtype=str).fillna("")
                    if "MARK" in df_check.columns:
                        marks_in_excel = df_check["MARK"].astype(str).str.strip().tolist()
                        if mark in marks_in_excel:
                            allow_edit_existing = True
            except Exception:
                log.exception("Could not read Excel to check duplicate MARK")

            # If not in cache, try receipt scraper to produce a single doc
            if not docs_for_mark and detect_and_scrape_receipt and not modal_warning:
                try:
                    rd = detect_and_scrape_receipt(mark)
                    if isinstance(rd, dict) and rd.get("MARK"):
                        docs_for_mark = [{
                            "mark": str(rd.get("MARK")),
                            "issueDate": rd.get("issue_date") or rd.get("issueDate") or "",
                            "totalValue": rd.get("total_amount") or rd.get("totalAmount") or rd.get("total_value") or "",
                            "AFM_issuer": rd.get("issuer_vat") or rd.get("issuer_afm") or "",
                            "Name_issuer": rd.get("issuer_name") or rd.get("issuer") or "",
                            "type": rd.get("doc_type") or "receipt",
                            "_scraper_source": rd.get("source") or "receipt_scraper",
                            "_is_receipt": True
                        }]
                        log.info("search: receipt scraper returned a doc for mark %s vat %s", mark, vat)
                except Exception:
                    log.exception("Receipt scraper failed for mark %s", mark)

            if not docs_for_mark:
                # ΜΗ βγάζεις error αν υπάρχει modal_warning — αφήνουμε το warning modal να εμφανιστεί
                if not modal_warning:
                    not_found_msg = f"MARK {mark} όχι στην cache του πελάτη {vat}. Κάνε πρώτα Fetch."
                    if is_ajax_search:
                        error = not_found_msg
                    else:
                        flash(not_found_msg, "error")
            else:
                if not classified_flag:
                    try:
                        # fiscal year check
                        sel_year = None
                        try:
                            sel_year = get_active_fiscal_year()
                        except Exception:
                            sel_year = None

                        first = docs_for_mark[0]
                        issue_date_str = pick(first, "issueDate", "issue_date", "date", "issueDate") or ""
                        issue_year = None
                        if issue_date_str and isinstance(issue_date_str, str):
                            m = re.search(r"\b(19|20)\d{2}\b", issue_date_str)
                            if m:
                                try:
                                    issue_year = int(m.group(0))
                                except Exception:
                                    issue_year = None
                        try:
                            sel_year_int = int(sel_year) if sel_year is not None else None
                        except Exception:
                            sel_year_int = None

                        if issue_year is not None and sel_year_int is not None and issue_year != sel_year_int:
                            modal_warning = (
                                f"Προσοχή: Το παραστατικό φαίνεται ότι εκδόθηκε το έτος {issue_year}, "
                                f"ενώ έχεις επιλεγμένη χρήση {sel_year_int}. Η διαδικασία μπλοκάρεται — "
                                "έλεγξε την ημερομηνία ή άλλαξε επιλεγμένη χρήση πριν συνεχίσεις."
                            )
                            fiscal_mismatch_block = True
                            log.info("search: fiscal year mismatch for MARK %s vat %s invoice_year=%s selected_year=%s", mark, vat, issue_year, sel_year_int)
                            # ΜΗΝ κάνουμε None το modal_summary - αφήνουμε να εμφανιστεί με warning
                            allow_edit_existing = False
                        # Συνεχίζουμε με invoice processing ακόμα και με mismatch
                        if True:  # Changed from 'else:' to always process
                            # =======================
                            #   HARD STOP ΓΙΑ ΤΙΜΟΛΟΓΙΑ
                            # =======================
                            if not expect_receipt:
                                FORBIDDEN_CODES = {"8.2", "8.4", "8.5", "8.6", "9.3", "11.1", "11.2", "11.5"}
                                FORBIDDEN_NAMES = {
                                    "Τέλος ανθεκτικότητας κλιματικής κρίσης",
                                    "Απόδειξη Είσπραξης POS",
                                    "Απόδειξη Επιστροφής POS",
                                    "Δελτίο Παραγγελίας Εστίασης",
                                    "Δελτίο Αποστολής",
                                    "ΑΛΠ",
                                    "ΑΠΥ",
                                    "Απόδειξη Λιανικής Πώλησης για Λογαριασμό Τρίτων",
                                }

                                forbidden_hit_code = None
                                forbidden_hit_name = None
                                try:
                                    for inst in docs_for_mark:
                                        code = str(pick(inst, "type", "invoiceType", "docType", "doc_type", default="")).strip()
                                        # ελληνική ονομασία από τον κωδικό (αν υπάρχει)
                                        name_guess = _map_invoice_type_local(code) if code else ""
                                        # μπορεί να έχει έρθει ήδη ως name
                                        name_field = str(pick(inst, "type_name", "doc_name", default="")).strip()

                                        if (code in FORBIDDEN_CODES) or (name_guess in FORBIDDEN_NAMES) or (name_field in FORBIDDEN_NAMES):
                                            forbidden_hit_code = code or None
                                            # Βάλε ως name προτεραιότητα: name_field -> name_guess -> mapper(code) -> code
                                            resolved_name = (
                                                name_field
                                                or name_guess
                                                or (_map_invoice_type_local(code) if code else "")
                                                or code
                                            )
                                            forbidden_hit_name = resolved_name
                                            break
                                except Exception:
                                    pass

                                if forbidden_hit_name:
                                    # --- ΝΕΟ: απόδοση ΟΠΩΣΔΗΠΟΤΕ σε ελληνική περιγραφή για το warning
                                    display_name = forbidden_hit_name
                                    try:
                                        # Αν αυτό που έχουμε μοιάζει με κωδικό, χαρτογράφησέ το
                                        if display_name == forbidden_hit_code or display_name.replace(".", "").isdigit():
                                            mapped = _map_invoice_type_local(forbidden_hit_code or display_name)
                                            if mapped:
                                                display_name = mapped
                                    except Exception:
                                        pass

                                    modal_warning = (
                                        f"Προσοχή: Το παραστατικό είναι {display_name}. "
                                        "Η ροή Τιμολογίων δεν το υποστηρίζει. Η διαδικασία μπλοκάρεται — "
                                        "έλεγξε το URL/MARK ή χρησιμοποίησε ροή Αποδείξεων εφόσον είναι κατάλληλο."
                                    )
                                    log.info(
                                        "search: invoice-flow blocked (forbidden type) code=%s name=%s mark=%s vat=%s",
                                        forbidden_hit_code, display_name, mark, vat
                                    )
                                    modal_summary = None
                                    invoice_lines = []
                                    allow_edit_existing = False

                            # Αν βάλαμε modal_warning από το guard, ΜΗ συνεχίσεις.
                            if modal_warning:
                                pass
                            else:
                                # detect receipts vs invoices
                                RECEIPT_TYPE_CODES = {"8.4", "8.5", "11.5"}
                                def is_receipt_doc(d):
                                    try:
                                        if d.get("_is_receipt"):
                                            return True
                                        t = str(pick(d, "type", "invoiceType", "docType", default="")).strip()
                                        if t in RECEIPT_TYPE_CODES or t.lower() in ("receipt", "apodeixis", "απόδειξη"):
                                            return True
                                        desc = str(pick(d, "description", "desc", "Name", "Name_issuer", default="")).lower()
                                        if "απόδειξη" in desc or "receipt" in desc or "λιανική" in desc or "pos" in desc:
                                            return True
                                        has_net = any(k in d for k in ("totalNetValue", "totalNet", "lineTotal", "net"))
                                        has_vat = any(k in d for k in ("totalVatAmount", "totalVat", "vat", "vatRate"))
                                        if (not has_net) and (("totalValue" in d) or ("amount" in d)) and (not has_vat):
                                            return True
                                    except Exception:
                                        pass
                                    return False

                                receipt_votes = sum(1 for dd in docs_for_mark if is_receipt_doc(dd))
                                is_group_receipt = (receipt_votes >= max(1, len(docs_for_mark) // 2))

                                if is_group_receipt:
                                    # Build receipt modal
                                    invoice_lines = []
                                    for idx, inst in enumerate(docs_for_mark):
                                        line_id = inst.get("id") or inst.get("line_id") or inst.get("LineId") or f"{mark}_inst{idx}"
                                        amount_raw = pick(inst, "totalValue", "amount", "lineTotal", "total", default=pick(inst, "value", "price", default=0))
                                        vat_raw = pick(inst, "totalVatAmount", "vat", "vatRate", default="")
                                        desc = pick(inst, "description", "desc", "Name", "note", default=f"Απόδειξη #{idx+1}")
                                        raw_vatcat = pick(inst, "vatCategory", "vat_category", "vatClass", "VATCategory", default="")
                                        mapped_vatcat = VAT_MAP.get(str(raw_vatcat).strip(), raw_vatcat) if raw_vatcat else ""
                                        invoice_lines.append({
                                            "id": line_id,
                                            "description": desc,
                                            "amount": amount_raw,
                                            "vat": vat_raw,
                                            "category": "",
                                            "vatCategory": mapped_vatcat
                                        })

                                    total_net = 0.0
                                    total_vat = 0.0
                                    total_value = 0.0
                                    for ln in invoice_lines:
                                        v = float_from_comma(ln.get("amount", 0))
                                        total_value += v
                                        vv = float_from_comma(ln.get("vat", 0))
                                        total_vat += vv
                                    total_net = total_value - total_vat if total_vat else total_value

                                    def fmt(x):
                                        try:
                                            return f"{x:.2f}".replace(".", ",")
                                        except Exception:
                                            return str(x)

                                    for ml in invoice_lines:
                                        try:
                                            ml_v = float_from_comma(ml.get("amount", 0))
                                            ml["amount"] = fmt(ml_v)
                                        except Exception:
                                            pass
                                        try:
                                            mv = float_from_comma(ml.get("vat", 0))
                                            ml["vat"] = fmt(mv)
                                        except Exception:
                                            pass

                                    # Enrich issuer name if missing
                                    issuer_afm = pick(docs_for_mark[0], "AFM_issuer", "AFM", default=vat)
                                    issuer_name = pick(docs_for_mark[0], "Name_issuer", "Name", default="")
                                    payment_method_type = ""
                                    for _doc in docs_for_mark:
                                        payment_method_type = str(pick(_doc, "paymentMethodType", default="") or "").strip()
                                        if payment_method_type:
                                            break
                                    
                                    # Try to enrich issuer name from client_db or VAT validator
                                    enriched_flag = False
                                    if not issuer_name or not str(issuer_name).strip():
                                        enriched_name = _enrich_issuer_name_from_afm(vat, issuer_afm, issuer_name)
                                        if enriched_name:
                                            issuer_name = enriched_name
                                            # Update the source document so it gets saved back
                                            docs_for_mark[0]["Name_issuer"] = enriched_name
                                            docs_for_mark[0]["Name"] = enriched_name
                                            enriched_flag = True
                                            # Save enriched data back to customer cache file
                                            try:
                                                customer_file = group_path(f"{vat}_invoices.json")
                                                for doc in cache:
                                                    if str(doc.get("mark", "")).strip() == mark:
                                                        doc["Name_issuer"] = enriched_name
                                                        doc["Name"] = enriched_name
                                                json_write(customer_file, cache)
                                                log.info("search: saved enriched issuer name to customer cache for mark %s", mark)
                                            except Exception:
                                                log.exception("Failed to save enriched issuer name to customer cache")
                                            # Save to Excel invoices
                                            try:
                                                update_issuer_name_in_excel(vat, mark, enriched_name)
                                                log.info("search: saved enriched issuer name to excel for mark %s", mark)
                                            except Exception:
                                                log.exception("Failed to save enriched issuer name to excel")
                                    
                                    modal_summary = {
                                        "mark": mark,
                                        "AA": pick(docs_for_mark[0], "AA", "aa", default=""),
                                        "AFM": issuer_afm,
                                        "Name": issuer_name,
                                        "paymentMethodType": payment_method_type,
                                        "paymentMethodLabel": get_payment_method_label(payment_method_type),
                                        "series": pick(docs_for_mark[0], "series", "Series", default=""),
                                        "number": pick(docs_for_mark[0], "number", "aa", "AA", default=""),
                                        "issueDate": pick(docs_for_mark[0], "issueDate", "issue_date", default=""),
                                        "totalNetValue": fmt(total_net),
                                        "totalVatAmount": fmt(total_vat),
                                        "totalValue": fmt(total_value),
                                        "type": "Απόδειξη",
                                        "type_name": "Απόδειξη",
                                        "docType": "receipt",
                                        "lines": invoice_lines,
                                        "is_receipt": True,
                                        "χαρακτηρισμός": "αποδειξακια"
                                    }

                                    # receipts should NOT show repeat-entry editing / categories
                                    customer_categories = []

                                    # prefill from epsilon if exists
                                    try:
                                        epsilon_path = os.path.join(group_path("epsilon"), f"{vat}_epsilon_invoices.json")
                                        eps_list = json_read(epsilon_path) or []
                                        matched = None
                                        for it in (eps_list or []):
                                            try:
                                                if str(it.get("mark", "")).strip() == str(mark).strip():
                                                    matched = it
                                                    break
                                            except Exception:
                                                continue
                                        if matched:
                                            modal_summary['χαρακτηρισμός'] = matched.get('χαρακτηρισμός') or matched.get('characteristic') or modal_summary.get('χαρακτηρισμός', '') or "αποδειξακια"
                                            # MTYPE είναι invoice-level - φορτώνουμε από matched root
                                            if matched.get("mtype"):
                                                modal_summary["mtype"] = matched.get("mtype")
                                            eps_lines = matched.get("lines", []) or []
                                            eps_by_id = {str(l.get("id", "")): l for l in eps_lines if l.get("id") is not None}
                                            for ml in modal_summary.get("lines", []):
                                                lid = str(ml.get("id", ""))
                                                if not lid:
                                                    continue
                                                eps_line = eps_by_id.get(lid)
                                                if eps_line:
                                                    if not ml.get("category") and eps_line.get("category"):
                                                        ml["category"] = eps_line.get("category")
                                                    if (not ml.get("vatCategory") or ml.get("vatCategory") == "") and eps_line.get("vat_category"):
                                                        ml["vatCategory"] = eps_line.get("vat_category")
                                    except Exception:
                                        log.exception("Could not prefill epsilon cache for receipts")

                                else:
                                    # Invoice flow (κανονικά)
                                    invoice_lines = []
                                    seen_vat_categories = set()
                                    for idx, inst in enumerate(docs_for_mark):
                                        line_id = inst.get("id") or inst.get("line_id") or inst.get("LineId") or f"{mark}_inst{idx}"
                                        description = pick(inst, "description", "desc", "Description", "Name", "Name_issuer") or f"Instance #{idx+1}"
                                        amount = pick(inst, "amount", "lineTotal", "totalNetValue", "totalValue", "value", default="")
                                        vat_rate = pick(inst, "vat", "vatRate", "vatPercent", "totalVatAmount", default="")
                                        raw_vatcat = pick(inst, "vatCategory", "vat_category", "vatClass", "vatCategoryCode", "VATCategory", "vatCat", default="")
                                        mapped_vatcat = VAT_MAP.get(str(raw_vatcat).strip(), raw_vatcat) if raw_vatcat else ""
                                        
                                        # Για Γ κατηγορία: αποφυγή διπλών γραμμών (κρατάμε μόνο την πρώτη εμφάνιση κάθε vatCategory)
                                        # Αυτό αποφεύγει τις επιπλέον γραμμές ΦΠΑ που εμφανίζονται σε τιμολόγια με πολλές γραμμές
                                        if mapped_vatcat and mapped_vatcat in seen_vat_categories:
                                            continue
                                        if mapped_vatcat:
                                            seen_vat_categories.add(mapped_vatcat)
                                        
                                        invoice_lines.append({
                                            "id": line_id,
                                            "description": description,
                                            "amount": amount,
                                            "vat": vat_rate,
                                            "category": "",
                                            "vatCategory": mapped_vatcat
                                        })

                                    first = docs_for_mark[0]
                                    total_net = sum(float_from_comma(pick(d, "totalNetValue", "totalNet", "lineTotal", default=0)) for d in docs_for_mark)
                                    total_vat = sum(float_from_comma(pick(d, "totalVatAmount", "totalVat", default=0)) for d in docs_for_mark)
                                    total_value = total_net + total_vat

                                    NEGATIVE_TYPES = {"5.1", "5.2", "11.4"}
                                    inv_type = str(pick(first, "type", "invoiceType", default="")).strip()
                                    is_negative = inv_type in NEGATIVE_TYPES
                                    type_mapped = mapper(inv_type)

                                    for ml in invoice_lines:
                                        try:
                                            v = float_from_comma(ml.get("amount", 0))
                                            ml["amount"] = f"{-abs(v):.2f}".replace(".", ",") if is_negative else f"{v:.2f}".replace(".", ",")
                                        except Exception:
                                            pass
                                        try:
                                            vv = float_from_comma(ml.get("vat", 0))
                                            ml["vat"] = f"{-abs(vv):.2f}".replace(".", ",") if is_negative else f"{vv:.2f}".replace(".", ",")
                                        except Exception:
                                            pass

                                    # Enrich issuer name if missing (for invoices)
                                    issuer_afm = pick(first, "AFM_issuer", "AFM_issuer", default=vat)
                                    issuer_name = pick(first, "Name", "Name_issuer", default="")
                                    payment_method_type = ""
                                    for _doc in docs_for_mark:
                                        payment_method_type = str(pick(_doc, "paymentMethodType", default="") or "").strip()
                                        if payment_method_type:
                                            break
                                    
                                    # Try to enrich issuer name from client_db or VAT validator
                                    enriched_flag = False
                                    if not issuer_name or not str(issuer_name).strip():
                                        enriched_name = _enrich_issuer_name_from_afm(vat, issuer_afm, issuer_name)
                                        if enriched_name:
                                            issuer_name = enriched_name
                                            # Update the source document so it gets saved back
                                            first["Name_issuer"] = enriched_name
                                            first["Name"] = enriched_name
                                            enriched_flag = True
                                            
                                            # Save enriched data back to customer cache file
                                            try:
                                                customer_file = group_path(f"{vat}_invoices.json")
                                                for doc in cache:
                                                    if str(doc.get("mark", "")).strip() == mark:
                                                        doc["Name_issuer"] = enriched_name
                                                        doc["Name"] = enriched_name
                                                json_write(customer_file, cache)
                                                log.info("search: saved enriched issuer name to customer cache for mark %s", mark)
                                            except Exception:
                                                log.exception("Failed to save enriched issuer name to customer cache")

                                    modal_summary = {
                                        "mark": mark,
                                        "AA": pick(first, "AA", "aa", default=""),
                                        "AFM": issuer_afm,
                                        "Name": issuer_name,
                                        "paymentMethodType": payment_method_type,
                                        "paymentMethodLabel": get_payment_method_label(payment_method_type),
                                        "series": pick(first, "series", "Series", "serie", default=""),
                                        "number": pick(first, "number", "aa", "AA", default=""),
                                        "issueDate": pick(first, "issueDate", "issue_date", default=pick(first, "issueDate", "issue_date", "")),
                                        "totalNetValue": (f"-{abs(total_net):.2f}" if is_negative else f"{total_net:.2f}").replace(".", ","),
                                        "totalVatAmount": (f"-{abs(total_vat):.2f}" if is_negative else f"{total_vat:.2f}").replace(".", ","),
                                        "totalValue": (f"-{abs(total_value):.2f}" if is_negative else f"{total_value:.2f}").replace(".", ","),
                                        "type": type_mapped,
                                        "type_code": inv_type,
                                        "type_name": type_mapped,
                                        "docType": "invoice",
                                        "lines": invoice_lines,
                                        "is_receipt": False
                                    }

                                    # epsilon prefill (όπως πριν)
                                    try:
                                        epsilon_path = os.path.join(group_path("epsilon"), f"{vat}_epsilon_invoices.json")
                                        eps_list = json_read(epsilon_path) or []

                                        if not eps_list:
                                            try:
                                                has_detail = False
                                                if modal_summary and (modal_summary.get("issueDate") or modal_summary.get("AFM") or modal_summary.get("AFM_issuer")):
                                                    has_detail = True
                                                if invoice_lines and any((ln.get("description") or ln.get("amount") or ln.get("vat")) for ln in invoice_lines):
                                                    has_detail = True
                                                if has_detail and modal_summary:
                                                    epsilon_entry = {
                                                        "mark": str(modal_summary.get("mark", "")).strip(),
                                                        "issueDate": modal_summary.get("issueDate", "") or "",
                                                        "series": modal_summary.get("series", "") or "",
                                                        "aa": modal_summary.get("number", "") or modal_summary.get("AA", ""),
                                                        "AA": modal_summary.get("number", "") or modal_summary.get("AA", ""),
                                                        "type": modal_summary.get("type_code", inv_type) or "",
                                                        "vatCategory": modal_summary.get("vatCategory", "") or "",
                                                        "totalNetValue": modal_summary.get("totalNetValue", "") or "",
                                                        "totalVatAmount": modal_summary.get("totalVatAmount", "") or "",
                                                        "totalValue": modal_summary.get("totalValue", "") or "",
                                                        "classification": modal_summary.get("classification", "") or "",
                                                        "χαρακτηρισμός": modal_summary.get("χαρακτηρισμός") or modal_summary.get("characteristic") or "",
                                                        "characteristic": modal_summary.get("χαρακτηρισμός") or modal_summary.get("characteristic") or "",
                                                        "AFM_issuer": modal_summary.get("AFM_issuer", "") or modal_summary.get("AFM", "") or "",
                                                        "Name_issuer": modal_summary.get("Name", "") or modal_summary.get("Name_issuer", "") or "",
                                                        "AFM": modal_summary.get("AFM", "") or vat,
                                                        "lines": []
                                                    }
                                                    for ln in invoice_lines:
                                                        epsilon_entry["lines"].append({
                                                            "id": ln.get("id", ""),
                                                            "description": ln.get("description", ""),
                                                            "amount": ln.get("amount", ""),
                                                            "vat": ln.get("vat", ""),
                                                            "category": ln.get("category", "") or "",
                                                            "vat_category": ln.get("vatCategory", "") or ""
                                                        })
                                                    eps_list.append(epsilon_entry)
                                                    try:
                                                        _safe_save_epsilon_cache(vat, eps_list)
                                                        log.info("Built per-mark epsilon entry for mark %s (vat %s)", epsilon_entry.get("mark"), vat)
                                                    except Exception:
                                                        log.exception("Failed to save newly built per-mark epsilon entry")
                                            except Exception:
                                                log.exception("Failed building per-mark epsilon entry")

                                        def match_epsilon_item(item, mark_val):
                                            for k in ('mark', 'MARK', 'invoice_id', 'Αριθμός Μητρώου', 'id'):
                                                if k in item and str(item[k]).strip() == str(mark_val).strip():
                                                    return True
                                            return False

                                        matched = None
                                        for it in (eps_list or []):
                                            try:
                                                if match_epsilon_item(it, mark):
                                                    matched = it
                                                    break
                                            except Exception:
                                                continue

                                        if matched:
                                            modal_summary['χαρακτηρισμός'] = matched.get('χαρακτηρισμός') or matched.get('characteristic') or modal_summary.get('χαρακτηρισμός', '') or ""
                                            # MTYPE είναι invoice-level - φορτώνουμε από matched root
                                            if matched.get("mtype"):
                                                modal_summary["mtype"] = matched.get("mtype")
                                            
                                            # Update epsilon cache with enriched issuer name if applicable
                                            if enriched_flag and issuer_name:
                                                try:
                                                    matched["Name_issuer"] = issuer_name
                                                    matched["Name"] = issuer_name
                                                    _safe_save_epsilon_cache(vat, eps_list)
                                                    log.info("search: saved enriched issuer name to epsilon cache for mark %s", mark)
                                                except Exception:
                                                    log.exception("Failed to save enriched issuer name to epsilon cache")
                                            
                                            try:
                                                eps_lines = matched.get("lines", []) or []
                                                eps_by_id = {str(l.get("id", "")): l for l in eps_lines if l.get("id") is not None}
                                                for ml in modal_summary.get("lines", []):
                                                    lid = str(ml.get("id", ""))
                                                    if not lid:
                                                        continue
                                                    eps_line = eps_by_id.get(lid)
                                                    if eps_line:
                                                        if not ml.get("category") and eps_line.get("category"):
                                                            ml["category"] = eps_line.get("category")
                                                        if (not ml.get("vatCategory") or ml.get("vatCategory") == "") and eps_line.get("vat_category"):
                                                            ml["vatCategory"] = eps_line.get("vat_category")
                                            except Exception:
                                                log.exception("Failed to merge per-line categories from epsilon")
                                    except Exception:
                                        log.exception("Could not read epsilon cache for prefill")

                    except Exception:
                        log.exception("search: fiscal year validation or modal build failed")
                        modal_summary = None
                        invoice_lines = []
                        try:
                            raw_tags = active_cred.get("expense_tags") if active_cred else None
                            if isinstance(raw_tags, str):
                                customer_categories = [t.strip() for t in raw_tags.split(",") if t.strip()]
                            elif isinstance(raw_tags, list):
                                customer_categories = raw_tags
                            if not customer_categories:
                                customer_categories = [
                                    "αγορες_εμπορευματων",
                                    "αγορες_α_υλων",
                                    "γενικες_δαπανες",
                                    "αμοιβες_τριτων",
                                    "δαπανες_χωρις_φπα"
                                ]
                        except Exception:
                            customer_categories = [
                                "αγορες_εμπορευματων",
                                "αγορες_α_υλων",
                                "γενικες_δαπανες",
                                "αμοιβες_τριτων",
                                "δαπανες_χωρις_φπα"
                            ]
                else:
                    modal_summary = None
                    invoice_lines = []
                    # categories remain default

    # ---------- Read credentials to extract repeat_entry config to pass to template ----------
    repeat_entry_conf = {}
    try:
        creds_list = read_credentials_list_local()
        idx = find_active_client_index_local(creds_list, vat_to_match=vat)
        if idx is not None and idx < len(creds_list):
            repeat_entry_conf = creds_list[idx].get("repeat_entry", {}) or {}
    except Exception:
        log.exception("Could not read repeat_entry config from credentials")

    # --- Build table_html from epsilon_invoices.json of active client ---
    try:
        active = get_active_credential_from_session() or {}
        active_vat = str(active.get("vat") or vat or "").strip()
        table_html, file_exists, _ = _render_table_html_for_vat(active_vat, with_checkbox_value=False)
    except Exception:
        log.exception("Failed building table_html for search page")
        table_html = ""
        file_exists = False

    try:
        force_edit_active = (
            request.args.get("force_edit") == "1"
            or request.form.get("force_edit") == "1"
        )
    except Exception:
        force_edit_active = False

    _prime_remote_summary_state(
        mark_value=mark,
        modal_summary=modal_summary if isinstance(modal_summary, dict) else None,
        invoice_lines=invoice_lines,
        categories=customer_categories,
        category_labels=customer_category_labels,
        warning_text=modal_warning,
        allow_edit_existing=allow_edit_existing,
        force_edit_active=force_edit_active,
    )

    if is_ajax_search and (request.method == "POST" or emulate_post):
        return jsonify({
            "ok": not bool(error),
            "error": error,
            "mark": mark,
            "modal_summary": modal_summary,
            "modal_warning": modal_warning,
            "classified": bool(classified_flag),
            "classified_message": classified_message,
            "allow_edit_existing": bool(allow_edit_existing),
            "customer_categories": customer_categories or [],
            "customer_category_labels": customer_category_labels or {},
            "category_vat_constraints": customer_vat_constraints or {},
            "fiscal_mismatch_block": bool(fiscal_mismatch_block),
            "scrape_url_is_receipt": bool(scrape_url_is_receipt),
            "receipt_custom_categories": receipt_custom_categories or [],
            "has_receipt_custom_categories": bool(has_receipt_custom_categories),
        }), (400 if error else 200)

    return safe_render(
        "search.html",
        result=result,
        error=error,
        mark=mark,
        modal_summary=modal_summary,
        invoice_lines=invoice_lines,
        customer_categories=customer_categories,
        customer_category_labels=customer_category_labels,
        allow_edit_existing=allow_edit_existing,
        vat=vat,
        active_page="search",
        table_html=strip_server_totals(table_html),
        file_exists=file_exists,
        css_numcols=css_numcols,
        modal_warning=modal_warning,
        scrape_url_is_receipt=scrape_url_is_receipt,
        fiscal_mismatch_block=fiscal_mismatch_block,
        repeat_entry_conf=repeat_entry_conf,
        active_year=active_year_val,
        category_vat_constraints=customer_vat_constraints,
        receipt_custom_categories=receipt_custom_categories,
        has_receipt_custom_categories=has_receipt_custom_categories,
        g_category_data=g_category_data,
    )
@app.get("/profiles")
def profiles_page():
    vat = (request.args.get("vat") or "").strip()
    mode = (request.args.get("mode") or "invoices").strip().lower()
    if mode not in ("invoices", "receipts"):
        mode = "invoices"

    creds = read_credentials_list()
    client = None
    if vat:
        client = _find_client(creds, vat=vat)
    if not client:
        try:
            active = get_active_credential_from_session() or {}
        except Exception:
            active = {}
        active_vat = str((active or {}).get("vat") or "").strip()
        if active_vat:
            client = _find_client(creds, vat=active_vat)
        if not client and isinstance(active, dict) and active:
            client = active
    client = client or {}

    # choose categories depending on mode
    if mode == "receipts":
        categories = _list_receipt_categories(client)
    else:
        categories = _list_invoice_categories(client)
    labels = _category_labels_for_client(client)
    constraints = _category_vat_constraints(client)
    raw_profiles = client.get("char_profiles", []) or []
    profiles = [p for p in _filter_char_profiles_by_mode(raw_profiles, mode) if not _is_general_profile(p)]

    g_category_data = None
    try:
        from g_category_helpers import (
            is_g_category_active,
            get_available_categories_for_g,
            enrich_categories_with_mtype,
        )

        if client and is_g_category_active(client):
            settings = load_settings()
            if settings:
                categories = get_available_categories_for_g(settings, categories)
                g_category_data = enrich_categories_with_mtype(categories, settings)
    except Exception:
        log.exception("profiles_page: failed to prepare g-category payload")
    # δίνουμε πάντα λίστα (όχι Undefined)
    return render_template(
        "profiles.html",
        vat=client.get("vat", "") or vat,
        mode=mode,
        customer_categories=categories,
        expense_tags=categories,
        profiles=profiles,
        category_labels=labels,
        vat_constraints=constraints,
        g_category_data=g_category_data,
    )


@app.route("/custom_categories", methods=["GET"])
def custom_categories_page():
    vat = (request.args.get("vat") or "").strip()
    active = get_active_credential_from_session() or {}
    if not vat:
        vat = str(active.get("vat") or "").strip()

    creds = load_credentials()
    client = None
    for c in creds:
        if str(c.get("vat") or "").strip() == vat:
            client = c
            break
    if client is None and active and str(active.get("vat") or "").strip() == vat:
        client = active

    categories = _custom_categories_payload(client)
    labels = _category_labels_for_client(client)
    constraints = _category_vat_constraints(client)
    client_name = str((client or {}).get("name") or "").strip()
    book_category = str((client or {}).get("book_category") or "Β").strip()
    return_name = (request.args.get("return_name") or client_name).strip()
    return_args: Dict[str, str] = {}
    if vat:
        return_args["vat"] = vat
    if return_name:
        return_args["return_name"] = return_name
    if return_args:
        return_args["open_custom"] = "1"
    return_url = url_for("credentials", **return_args)
    return render_template(
        "custom_categories.html",
        vat=vat,
        categories=categories,
        category_labels=labels,
        default_labels=DEFAULT_INVOICE_CATEGORY_LABELS,
        vat_rates=VAT_RATE_NUMERIC,
        vat_constraints=constraints,
        credential_name=client_name,
        book_category=book_category,
        return_url=return_url,
        active_page="custom_categories",
    )


@app.post("/custom_categories/save")
def custom_categories_save():
    vat = str(request.form.get("vat") or "").strip()
    label = str(request.form.get("label") or "").strip()
    slug = str(request.form.get("id") or "").strip()
    enabled = request.form.get("enabled") in ("on", "true", "1")
    applies_to_receipts = request.form.get("applies_to_receipts") in ("on", "true", "1")

    if not vat:
        flash("Επιλογή πελάτη (VAT) απαιτείται.", "error")
        return redirect(url_for("custom_categories_page"))

    creds = load_credentials()
    client = next((c for c in creds if str(c.get("vat") or "").strip() == vat), None)
    if not client:
        flash("Ο πελάτης δεν βρέθηκε.", "error")
        return redirect(url_for("custom_categories_page", vat=vat))

    client_name = str(client.get("name") or "").strip()
    existing_slugs = {str(cat.get("id") or cat.get("slug") or "").strip() for cat in _ensure_custom_categories_list(client)}
    if slug:
        target = None
        for cat in _ensure_custom_categories_list(client):
            if str(cat.get("id") or cat.get("slug") or "").strip() == slug:
                target = cat
                break
        if target is None:
            existing_slugs.discard(slug)
            target = {"id": slug}
            _ensure_custom_categories_list(client).append(target)
    else:
        if not label:
            flash("Συμπλήρωσε τίτλο κατηγορίας.", "error")
            return redirect(url_for("custom_categories_page", vat=vat, return_name=client_name))
        slug = _slugify_custom_category(label, existing_slugs)
        target = {"id": slug}
        _ensure_custom_categories_list(client).append(target)

    if label:
        target["label"] = label
    target["enabled"] = bool(enabled)
    target["applies_to_receipts"] = bool(applies_to_receipts)
    
    # Get book_category for validation
    book_category = str(client.get("book_category") or "Β").strip().upper()
    
    # Collect and validate accounts
    accounts = {}
    validation_errors = []
    for rate in VAT_RATE_NUMERIC:
        account_code = str(request.form.get(f"account_{rate}") or "").strip()
        
        # Validate format if not empty
        if account_code:
            # Remove all non-digit and non-dash characters for validation
            cleaned = ''.join(c for c in account_code if c.isdigit() or c == '-')
            digits_only = ''.join(c for c in account_code if c.isdigit())
            
            if book_category in ('Γ', 'G'):
                # Γ Category: xx-xx-xx-xxxx (10 digits total)
                if not re.match(r'^\d{2}-\d{2}-\d{2}-\d{4}$', cleaned) or len(digits_only) != 10:
                    validation_errors.append(f"ΦΠΑ {rate}%: Μη έγκυρη μορφή (αναμένεται xx-xx-xx-xxxx με 10 ψηφία)")
            else:
                # Β Category: xx-xxxx (6 digits total)
                if not re.match(r'^\d{2}-\d{4}$', cleaned) or len(digits_only) != 6:
                    validation_errors.append(f"ΦΠΑ {rate}%: Μη έγκυρη μορφή (αναμένεται xx-xxxx με 6 ψηφία)")
        
        accounts[rate] = account_code
    
    # If validation errors, flash and redirect back
    if validation_errors:
        for error in validation_errors:
            flash(error, "error")
        expected_format = "xx-xx-xx-xxxx (10 ψηφία)" if book_category in ('Γ', 'G') else "xx-xxxx (6 ψηφία)"
        flash(f"Αναμενόμενη μορφή λογαριασμών για {book_category} κατηγορία: {expected_format}", "info")
        return redirect(url_for("custom_categories_page", vat=vat, return_name=client_name))
    
    # Validate account codes exist in chart_of_accounts.xlsx (Β ή Γ ανάλογα με κατηγορία)
    chart_filename = 'chart_of_accounts_g.xlsx' if book_category in ('Γ', 'G') else 'chart_of_accounts_b.xlsx'
    chart_path = os.path.join(get_group_base_dir(), chart_filename)
    if os.path.exists(chart_path):
        try:
            df = pd.read_excel(chart_path, dtype=str)
            # Get valid account codes from 'Κωδικός' column
            valid_codes = set(df['Κωδικός'].dropna().astype(str).str.strip())
            
            # Check each non-empty account
            invalid_accounts = []
            for rate, account_code in accounts.items():
                if account_code and account_code not in valid_codes:
                    invalid_accounts.append(f"ΦΠΑ {rate}%: Ο λογαριασμός '{account_code}' δεν υπάρχει στο Λογιστικό Σχέδιο")
            
            if invalid_accounts:
                for error in invalid_accounts:
                    flash(error, "error")
                flash(f"Οι λογαριασμοί πρέπει να υπάρχουν στο {chart_filename}", "warning")
                return redirect(url_for("custom_categories_page", vat=vat, return_name=client_name))
        except Exception as e:
            current_app.logger.warning(f"Failed to validate against {chart_filename}: {e}")
            # Continue with save - chart validation is optional if file has issues
    
    # Keep the receipts flag BOTH at top-level and inside accounts metadata,
    # so it survives any flow that may persist only account blocks.
    accounts_with_meta = dict(accounts)
    accounts_with_meta["__applies_to_receipts"] = bool(applies_to_receipts)
    target["accounts"] = accounts_with_meta

    save_credentials(creds)
    flash("Η κατηγορία αποθηκεύτηκε.", "success")
    return redirect(url_for("custom_categories_page", vat=vat, return_name=client_name))


@app.post("/custom_categories/delete")
def custom_categories_delete():
    vat = str(request.form.get("vat") or "").strip()
    slug = str(request.form.get("id") or "").strip()
    if not (vat and slug):
        flash("Ανεπαρκή στοιχεία διαγραφής.", "error")
        return redirect(url_for("custom_categories_page", vat=vat))

    creds = load_credentials()
    client = next((c for c in creds if str(c.get("vat") or "").strip() == vat), None)
    if not client:
        flash("Ο πελάτης δεν βρέθηκε.", "error")
        return redirect(url_for("custom_categories_page", vat=vat))

    client_name = str(client.get("name") or "").strip()
    arr = _ensure_custom_categories_list(client)
    new_arr = [cat for cat in arr if str(cat.get("id") or cat.get("slug") or "").strip() != slug]
    client["custom_categories"] = new_arr

    # καθάρισε repeat mapping που δείχνουν σε αυτή την κατηγορία
    repeat = client.get("repeat_entry")
    if isinstance(repeat, dict):
        mapping = repeat.get("mapping") or {}
        for key, val in list(mapping.items()):
            if str(val).strip() == slug:
                mapping[key] = ""
        repeat["mapping"] = mapping
        client["repeat_entry"] = repeat

    # καθάρισε char_profiles
    profiles = client.get("char_profiles")
    if isinstance(profiles, list):
        for prof in profiles:
            if not isinstance(prof, dict):
                continue
            mp = prof.get("mapping")
            if isinstance(mp, dict):
                for key, val in list(mp.items()):
                    if str(val).strip() == slug:
                        mp[key] = ""

    save_credentials(creds)
    flash("Η κατηγορία διαγράφηκε.", "success")
    return redirect(url_for("custom_categories_page", vat=vat, return_name=client_name))

@app.get("/api/char_profiles")
def api_char_profiles_get():
    """Επιστρέφει profiles + expense_tags για τον ενεργό πελάτη"""
    vat = request.args.get("vat","").strip()
    mode = (request.args.get("mode") or request.args.get("flow") or "").strip().lower()
    if mode not in ("invoices", "receipts"):
        mode = "invoices"

    def _find_client_in_group_files(vat_value: str = "", name_value: str = ""):
        try:
            for cand in Path(DATA_DIR).glob('*/credentials.json'):
                try:
                    with cand.open('r', encoding='utf-8') as f:
                        arr = json.load(f) or []
                except Exception:
                    continue
                found = _find_client(arr, vat=vat_value or None, name=name_value or None)
                if found:
                    return found, arr, cand
        except Exception:
            pass
        return None, None, None

    creds = read_credentials_list()
    client = _find_client(creds, vat=vat) if vat else None
    # Fallback: resolve active credential robustly (vat OR name), then fallback
    # to session snapshot only if full record cannot be found.
    if not client:
        try:
            active = get_active_credential_from_session() or {}
        except Exception:
            active = {}
        active_vat = str((active or {}).get("vat") or (active or {}).get("afm") or "").strip()
        active_name = str((active or {}).get("name") or "").strip()
        if active_vat:
            client = _find_client(creds, vat=active_vat)
        if not client and active_name:
            client = _find_client(creds, name=active_name)
        if not client and isinstance(active, dict) and active:
            client = active
        if not client and (vat or active_name):
            group_client, _, _ = _find_client_in_group_files(vat, active_name)
            if group_client:
                client = group_client
    client = client or {}
    raw_profiles = client.get("char_profiles", []) or []
    profiles = _filter_char_profiles_by_mode(raw_profiles, mode)

    expense_tags = _list_receipt_categories(client) if mode == "receipts" else _list_invoice_categories(client)
    receipt_expense_tags = _list_receipt_categories(client)
    labels = _category_labels_for_client(client)
    constraints = _category_vat_constraints(client)
    return jsonify(
        ok=True,
        profiles=profiles,
        expense_tags=expense_tags,
        receipt_expense_tags=receipt_expense_tags,
        options=expense_tags,
        category_labels=labels,
        vat_constraints=constraints,
    )

@app.post("/api/char_profiles/save")
def api_char_profiles_save():
    """
    Body: { vat, name, mapping: {kat_fpa_a, kat_fpa_b, kat_fpa_g, kat_fpa_d} }
    Αν υπάρχει προφίλ με το ίδιο όνομα -> update, αλλιώς append.
    """
    data = request.get_json(force=True, silent=True) or {}
    vat = str(data.get("vat", "")).strip()
    name = (data.get("name") or "").strip()
    mapping = data.get("mapping") or {}
    mode = (data.get("mode") or "invoices").strip().lower()
    if mode not in ("invoices", "receipts"):
        mode = "invoices"
    invoice_mtype = str(data.get("invoice_mtype") or "").strip()
    receipt_mtype = str(data.get("receipt_mtype") or "").strip()

    if mode == "receipts" and not receipt_mtype and invoice_mtype:
        receipt_mtype = invoice_mtype

    # If vat not provided, try to use session active credential
    active_name = ""
    if not vat:
        try:
            active = get_active_credential_from_session() or {}
            vat = (active.get("vat") or "").strip()
            active_name = (active.get("name") or "").strip()
        except Exception:
            vat = ""
            active_name = ""

    # allow empty name for receipts mode (generic profile)
    if not all(mapping.get(k) for k in ("kat_fpa_a", "kat_fpa_b", "kat_fpa_g", "kat_fpa_d", "kat_fpa_e")):
        return jsonify(ok=False, error="Παράμετροι λείπουν"), 400
    if not name and mode != 'receipts':
        # invoices still require a named profile to avoid ambiguity
        return jsonify(ok=False, error="Απαιτείται όνομα προφίλ για τιμολόγια"), 400

    def _find_client_in_group_files(vat_value: str = "", name_value: str = ""):
        try:
            for cand in Path(DATA_DIR).glob('*/credentials.json'):
                try:
                    with cand.open('r', encoding='utf-8') as f:
                        arr = json.load(f) or []
                except Exception:
                    continue
                found = _find_client(arr, vat=vat_value or None, name=name_value or None)
                if found:
                    return found, arr, cand
        except Exception:
            pass
        return None, None, None

    with CREDENTIALS_RW_LOCK:
        creds = read_credentials_list()
        save_path = None
        client = _find_client(creds, vat=vat) if vat else None
        if not client and active_name:
            client = _find_client(creds, name=active_name)
        if not client and (vat or active_name):
            group_client, group_creds, group_path = _find_client_in_group_files(vat, active_name)
            if (
                group_client is not None
                and group_creds is not None
                and group_path is not None
            ):
                client = group_client
                creds = group_creds
                save_path = group_path
        # Fallback: if lookup by vat failed, use session active credential object
        if not client:
            try:
                client = get_active_credential_from_session() or None
            except Exception:
                client = None
        if not client:
            return jsonify(ok=False, error="Δεν βρέθηκε πελάτης"), 404

        constraints = _category_vat_constraints(client)
        labels = _category_labels_for_client(client)
        vat_by_key = {
            "kat_fpa_a": "0%",
            "kat_fpa_b": "6%",
            "kat_fpa_g": "13%",
            "kat_fpa_d": "17%",
            "kat_fpa_e": "24%",
        }
        for key, vat_label in vat_by_key.items():
            val = str(mapping.get(key) or "").strip()
            if not val:
                continue
            allowed = constraints.get(val)
            if allowed is not None and vat_label not in allowed:
                display = labels.get(val, val)
                return jsonify(
                    ok=False,
                    error=f"Η κατηγορία '{display}' δεν υποστηρίζει ΦΠΑ {vat_label}.",
                ), 400

        arr = list(client.get("char_profiles", []))
        # upsert by name+mode, but also migrate legacy same-name profiles without mode
        hit = None
        for p in arr:
            if not isinstance(p, dict):
                continue
            p_name = str(p.get("name", "")).strip().lower()
            p_mode = str(p.get("mode") or "").strip().lower()
            if p_name != name.lower():
                continue
            if p_mode == mode or (mode == "receipts" and p_mode == ""):
                hit = p
                break
        if hit:
            hit["mapping"] = mapping
            hit.pop("map", None)
            hit["mode"] = mode
            hit["invoice_mtype"] = invoice_mtype
            hit["receipt_mtype"] = receipt_mtype
        else:
            arr.append({
                "name": name,
                "mapping": mapping,
                "mode": mode,
                "invoice_mtype": invoice_mtype,
                "receipt_mtype": receipt_mtype,
            })
        client["char_profiles"] = arr
        if save_path is not None:
            try:
                with save_path.open('w', encoding='utf-8') as f:
                    json.dump(creds, f, ensure_ascii=False, indent=2)
            except Exception:
                write_credentials_list(creds)
        else:
            write_credentials_list(creds)
    return jsonify(
        ok=True,
        profile={
            "name": name,
            "mapping": mapping,
            "mode": mode,
            "invoice_mtype": invoice_mtype,
            "receipt_mtype": receipt_mtype,
        },
    )
# ------- repeat entry mapping (πάντα ποσοστά) -------



@app.post("/api/char_profiles/delete")
def api_char_profiles_delete():
    data = request.get_json(force=True, silent=True) or {}
    vat = str(data.get("vat","")).strip()
    name = (data.get("name") or "").strip()
    mode = (data.get("mode") or "invoices").strip().lower()
    if mode not in ("invoices", "receipts"):
        mode = "invoices"
    def _find_client_in_group_files(vat_value: str = "", name_value: str = ""):
        try:
            for cand in Path(DATA_DIR).glob('*/credentials.json'):
                try:
                    with cand.open('r', encoding='utf-8') as f:
                        arr = json.load(f) or []
                except Exception:
                    continue
                found = _find_client(arr, vat=vat_value or None, name=name_value or None)
                if found:
                    return found, arr, cand
        except Exception:
            pass
        return None, None, None

    with CREDENTIALS_RW_LOCK:
        creds = read_credentials_list()
        save_path = None
        client = _find_client(creds, vat=vat) if vat else None
        if not client:
            try:
                active = get_active_credential_from_session() or {}
            except Exception:
                active = {}
            active_vat = str((active or {}).get("vat") or (active or {}).get("afm") or "").strip()
            active_name = str((active or {}).get("name") or "").strip()
            if active_vat:
                client = _find_client(creds, vat=active_vat)
            if not client and active_name:
                client = _find_client(creds, name=active_name)
            if not client and (vat or active_name):
                group_client, group_creds, group_path = _find_client_in_group_files(vat, active_name)
                if (
                    group_client is not None
                    and group_creds is not None
                    and group_path is not None
                ):
                    client = group_client
                    creds = group_creds
                    save_path = group_path
        if not client:
            return jsonify(ok=False, error="Δεν βρέθηκε πελάτης"), 404
        arr = list(client.get("char_profiles", []))
        # remove matching name+mode; for receipts also remove legacy no-mode profile
        def _keep_profile(p):
            if not isinstance(p, dict):
                return True
            p_name = str(p.get("name", "")).strip().lower()
            p_mode = str(p.get("mode") or "").strip().lower()
            if p_name != name.lower():
                return True
            if mode == "receipts":
                return p_mode not in ("receipts", "")
            return p_mode != "invoices"

        arr = [p for p in arr if _keep_profile(p)]
        client["char_profiles"] = arr
        if save_path is not None:
            try:
                with save_path.open('w', encoding='utf-8') as f:
                    json.dump(creds, f, ensure_ascii=False, indent=2)
            except Exception:
                write_credentials_list(creds)
        else:
            write_credentials_list(creds)
    return jsonify(ok=True)

@app.get("/profiles", endpoint="char_profiles_ui")
def char_profiles_ui():
    return profiles_page()
@app.route("/api/next_receipt_mark", methods=["GET"])
def api_next_receipt_mark():
    """
    Επιστρέφει ένα 15ψήφιο επόμενο MARK για αποδείξεις.
    Βασίζεται στον ενεργό credential (get_active_credential_from_session),
    και ψάχνει υπάρχοντα MARKs σε:
      - excel_path_for(vat)
      - DATA_DIR/epsilon/<vat>_epsilon_invoices.json
      - DATA_DIR/<vat>_invoices.json
    Αν δεν βρεθεί τίποτα, ξεκινάει από DEFAULT_BASE_MARK (400000000000000).
    """
    try:
        # βοηθητικά
        def norm_mark_str(s):
            if s is None: return None
            s = str(s).strip()
            m = re.search(r"\d{15}", s)
            if m:
                return m.group(0)
            # try numeric conversion
            digits = re.sub(r"\D", "", s)
            if len(digits) >= 15:
                return digits[:15]
            if digits:
                return digits.zfill(15)
            return None

        # attempt to determine active VAT just like other endpoints
        vat = None
        try:
            active = get_active_credential_from_session()
            vat = active.get("vat") if active else None
        except Exception:
            vat = None

        existing_marks = set()

        # 1) read excel if exists
        try:
            if vat:
                path = excel_path_for(vat=vat)
                if path and os.path.exists(path):
                    import pandas as pd
                    df = pd.read_excel(path, engine="openpyxl", dtype=str).fillna("")
                    if "MARK" in df.columns:
                        for v in df["MARK"].astype(str).tolist():
                            s = norm_mark_str(v)
                            if s: existing_marks.add(s)
        except Exception:
            log.exception("api_next_receipt_mark: failed reading excel")

        # 2) read epsilon json for vat
        try:
            if vat:
                eps_path = os.path.join(group_path("epsilon"), f"{vat}_epsilon_invoices.json")
                if os.path.exists(eps_path):
                    try:
                        with open(eps_path, "r", encoding="utf-8") as f:
                            eps = json.load(f) or []
                        # eps might be list or dict
                        if isinstance(eps, dict):
                            # older shapes: keys maybe marks
                            for k in eps.keys():
                                s = norm_mark_str(k)
                                if s: existing_marks.add(s)
                            # also values may contain 'mark' fields
                            for v in eps.values():
                                try:
                                    if isinstance(v, dict):
                                        s = norm_mark_str(v.get("mark") or v.get("MARK") or v.get("invoice_id"))
                                        if s: existing_marks.add(s)
                                except Exception:
                                    pass
                        else:
                            for it in (eps or []):
                                try:
                                    if isinstance(it, dict):
                                        s = norm_mark_str(it.get("mark") or it.get("MARK") or it.get("invoice_id") or "")
                                        if s: existing_marks.add(s)
                                except Exception:
                                    pass
                    except Exception:
                        log.exception("api_next_receipt_mark: failed parsing epsilon json %s", eps_path)
        except Exception:
            log.exception("api_next_receipt_mark: epsilon read error")

        # 3) read cached invoices file
        try:
            if vat:
                cust_file = group_path(f"{vat}_invoices.json")
                if os.path.exists(cust_file):
                    try:
                        j = json_read(cust_file) or []
                        if isinstance(j, dict):
                            # if dict map by mark
                            for k in j.keys():
                                s = norm_mark_str(k)
                                if s: existing_marks.add(s)
                        else:
                            for it in (j or []):
                                try:
                                    if isinstance(it, dict):
                                        s = norm_mark_str(it.get("mark") or it.get("MARK") or "")
                                        if s: existing_marks.add(s)
                                except Exception:
                                    pass
                    except Exception:
                        log.exception("api_next_receipt_mark: failed parsing customer_file %s", cust_file)
        except Exception:
            log.exception("api_next_receipt_mark: cached invoices read error")

        # Convert to integers safely
        numeric_marks = []
        for m in existing_marks:
            try:
                numeric_marks.append(int(m))
            except Exception:
                continue

        DEFAULT_BASE_MARK = 500000000000000  # safe starting point if none found

        if numeric_marks:
            next_num = max(numeric_marks) + 1
            if next_num < DEFAULT_BASE_MARK:
                next_num = DEFAULT_BASE_MARK
        else:
            # try to seed from AFM or other heuristic — keep it deterministic
            try:
                if vat and vat.isdigit() and len(vat) <= 9:
                    # create a semi-deterministic seed: 400 + last digits of vat padded
                    seed_tail = vat.zfill(9)[-9:]
                    # simple mix to produce 15 digits: 400 + seed_tail + '00'
                    candidate = f"4{seed_tail}"  # might be <15
                    candidate = re.sub(r'\D', '', candidate).zfill(15)
                    next_num = int(candidate)
                    if next_num <= DEFAULT_BASE_MARK:
                        next_num = DEFAULT_BASE_MARK
                else:
                    next_num = DEFAULT_BASE_MARK
            except Exception:
                next_num = DEFAULT_BASE_MARK

        # ensure 15-digit string
        next_mark_str = str(next_num).zfill(15)
        if len(next_mark_str) > 15:
            next_mark_str = next_mark_str[-15:]

        return jsonify({"ok": True, "mark": next_mark_str})
    except Exception as e:
        log.exception("api_next_receipt_mark: unexpected")
        return jsonify({"ok": False, "error": str(e)}), 500

# --- Paste αυτό το endpoint μέσα στο app.py (δίπλα στα υπόλοιπα /api endpoints) ---
@app.route("/api/scrape_receipt", methods=["POST"])
def api_scrape_receipt():
    """
    Αναμένει JSON: { url: "<receipt url>" }
    Επιστρέφει: { ok: True, is_invoice: bool, mark: "<15digits or ''>", issue_date: "...", total_amount: "...", issuer_vat: "...", issuer_name: "...", progressive_aa: "...", raw: <original dict> }
    """
    try:
        data = request.get_json(silent=True) or {}
        url = data.get("url") or data.get("mark") or data.get("q") or ""
        mode = str(data.get("mode") or "mixed").strip().lower()
        if mode not in ("analysis", "mixed"):
            mode = "mixed"
        if not url:
            return jsonify({"ok": False, "error": "Missing 'url' in request"}), 400

        log.info("api_scrape_receipt: incoming url=%s mode=%s", url, mode)

        # analysis -> scraper_receipt_analysis.py
        # mixed    -> scraper_receipt.py
        detect_and_scrape = None
        if mode == "analysis":
            try:
                from scraper_receipt_analysis import detect_and_scrape as detect_and_scrape
            except Exception:
                log.exception("api_scrape_receipt: cannot import analysis scraper, fallback to legacy")
                try:
                    from scraper_receipt import detect_and_scrape as detect_and_scrape
                except Exception:
                    log.exception("api_scrape_receipt: cannot import any detect_and_scrape")
                    return jsonify({"ok": False, "error": "No receipt scraper available"}), 500
        else:
            try:
                from scraper_receipt import detect_and_scrape as detect_and_scrape
            except Exception:
                log.exception("api_scrape_receipt: cannot import mixed scraper, fallback to analysis")
                try:
                    from scraper_receipt_analysis import detect_and_scrape as detect_and_scrape
                except Exception:
                    log.exception("api_scrape_receipt: cannot import any detect_and_scrape")
                    return jsonify({"ok": False, "error": "No receipt scraper available"}), 500

        scraped = detect_and_scrape(url)
        # normalize expected shape (best-effort)
        if not scraped or not isinstance(scraped, dict):
            return jsonify({"ok": False, "error": "scraper returned unexpected result"}), 500

        # Example fields in your scraper_receipt output: MARK, doc_type, is_invoice, issue_date, issuer_name, issuer_vat, progressive_aa, total_amount, raw
        is_invoice = bool(scraped.get("is_invoice")) or False
        mark = str(scraped.get("MARK") or scraped.get("mark") or "").strip()
        issue_date = scraped.get("issue_date") or scraped.get("issueDate") or scraped.get("date") or ""
        total_amount = str(scraped.get("total_amount") or scraped.get("totalValue") or scraped.get("total") or scraped.get("totalValueGross") or "")
        issuer_vat = scraped.get("issuer_vat") or scraped.get("issuer_vat") or scraped.get("issuerAfm") or scraped.get("ΑΦΜ") or scraped.get("AFM") or ""
        issuer_name = scraped.get("issuer_name") or scraped.get("issuerName") or scraped.get("Name") or ""
        progressive_aa = scraped.get("progressive_aa") or scraped.get("AA") or scraped.get("aa") or ""
        vat_analysis = scraped.get("vat_analysis") if isinstance(scraped.get("vat_analysis"), dict) else {}
        vat_analysis_inferred = bool(scraped.get("vat_analysis_inferred", False))

        receipt_analysis = scraped.get("receipt_analysis") if isinstance(scraped.get("receipt_analysis"), list) else []
        if not receipt_analysis and vat_analysis:
            def _rate_sort_key(rate_key):
                try:
                    return -float(str(rate_key).replace('%', '').replace(',', '.'))
                except Exception:
                    return 0.0

            for idx, rate in enumerate(sorted([k for k in vat_analysis.keys() if k != "__inferred__"], key=_rate_sort_key)):
                row = vat_analysis.get(rate) or {}
                rate_text = str(rate)
                if rate_text and '%' not in rate_text:
                    rate_text = f"{rate_text}%"
                receipt_analysis.append({
                    "id": f"r{idx}",
                    "vatCategory": rate_text,
                    "amount": row.get("net_amount") or row.get("gross_amount") or "",
                    "vat": row.get("vat_amount") or "",
                    "description": "",
                    "category": ""
                })

        # If scraper MARK is missing/invalid, assign unique pseudo-MARK for receipts.
        if not re.fullmatch(r"\d{15}", mark or ""):
            generated_mark = ""
            try:
                next_resp = api_next_receipt_mark()
                next_http = next_resp[0] if isinstance(next_resp, tuple) else next_resp
                next_payload = next_http.get_json(silent=True) if hasattr(next_http, "get_json") else None
                candidate = str((next_payload or {}).get("mark") or "").strip()
                if re.fullmatch(r"\d{15}", candidate):
                    generated_mark = candidate
            except Exception:
                log.exception("api_scrape_receipt: fallback mark generation via api_next_receipt_mark failed")

            if not generated_mark:
                generated_mark = RECEIPT_FALLBACK_MARK

            try:
                last_generated = str(session.get("_last_generated_receipt_mark") or "").strip()
                if re.fullmatch(r"\d{15}", last_generated) and re.fullmatch(r"\d{15}", generated_mark):
                    next_num = max(int(generated_mark), int(last_generated) + 1)
                    generated_mark = str(next_num).zfill(15)
                    if len(generated_mark) > 15:
                        generated_mark = generated_mark[-15:]
                session["_last_generated_receipt_mark"] = generated_mark
            except Exception:
                log.exception("api_scrape_receipt: could not persist last generated fallback mark")

            mark = generated_mark

        log.info("api_scrape_receipt: scraped url=%s mode=%s is_invoice=%s mark=%s", url, mode, is_invoice, mark)

        return jsonify({
            "ok": True,
            "mode": mode,
            "is_invoice": bool(is_invoice),
            "mark": mark,
            "issue_date": issue_date,
            "total_amount": total_amount,
            "issuer_vat": issuer_vat,
            "issuer_name": issuer_name,
            "progressive_aa": progressive_aa,
            "receipt_analysis": receipt_analysis,
            "vat_analysis": vat_analysis,
            "vat_analysis_inferred": vat_analysis_inferred,
            "raw": scraped
        })
    except Exception as e:
        log.exception("api_scrape_receipt: unexpected")
        return jsonify({"ok": False, "error": str(e)}), 500




@app.route("/save_epsilon", methods=["POST"])
def save_epsilon():
    """
    Παρέχεται για απευθείας αποθήκευση epsilon (αν θέλεις ξεχωριστό κουμπί).
    Αναμένει form field "summary_json" (όπως το modal στέλνει) και αποθηκεύει
    το αντικείμενο στο per-vat epsilon file (data/epsilon/{vat}_epsilon_invoices.json).
    """
    active_cred = get_active_credential_from_session()
    if not active_cred:
        if not session.get('active_group'):
            flash("Δεν έχει επιλεγεί ενεργή ομάδα. Επίλεξε πρώτα ομάδα.", "error")
        else:
            flash("Δεν υπάρχει ενεργός πελάτης για αποθήκευση.", "error")
        return redirect(url_for("search"))

    vat = active_cred.get("vat")
    if not vat:
        flash("Δεν βρέθηκε ΑΦΜ πελάτη.", "error")
        return redirect(url_for("search"))

    summary_json = request.form.get("summary_json")
    if not summary_json:
        flash("Δεν στάλθηκε δεδομένο για αποθήκευση.", "error")
        return redirect(url_for("search"))

    try:
        summary_data = json.loads(summary_json)
    except Exception as e:
        flash(f"Σφάλμα κατά την ανάγνωση του JSON: {e}", "error")
        return redirect(url_for("search"))

    # load existing epsilon cache and update per MARK
    epsilon_cache = load_epsilon_cache_for_vat(vat)

    mark = str(summary_data.get("mark", ""))
    existing_index = next((i for i, d in enumerate(epsilon_cache) if str(d.get("mark","")) == mark), None)
    if existing_index is not None:
        epsilon_cache[existing_index] = summary_data
    else:
        epsilon_cache.append(summary_data)

    # Save to disk
    try:
        save_epsilon_cache_for_vat(vat, epsilon_cache)
    except Exception:
        flash("Αποτυχία αποθήκευσης Epsilon αρχείου.", "error")
        return redirect(url_for("search"))

    flash(f"Το παραστατικό MARK {mark} αποθηκεύτηκε για Epsilon Excel.", "success")
    return redirect(url_for("search"))


@app.route("/save_accounts", methods=["POST"])
def save_accounts():
    """
    Αναμένει POST με πεδία:
      - account_<vat>__<expense_tag> = account_code
      - account_g_cash = cash_account_code  (Γ-category specific)
    Παράδειγμα πεδίου: account_24__γενικες_δαπανες = "70.02"
    Επιπλέον μπορεί να στέλνεται JSON payload.
    """
    try:
        # αν JSON payload
        if request.is_json:
            payload = request.get_json()
            accounts = payload.get("accounts", {})
            save_global_accounts_to_credentials(accounts)
            
            # Handle account_g_cash from JSON
            cash_acct = payload.get("account_g_cash", "").strip()
            if cash_acct:
                settings = load_settings() or {}
                settings["account_g_cash"] = cash_acct
                save_settings(settings)
            
            flash("Οι γενικοί λογαριασμοί αποθηκεύτηκαν", "success")
            return redirect(url_for("credentials"))
        
        # αλλιώς form fields
        form = request.form
        
        # Δομή: accounts[vat][expense_tag] = code
        accounts = {}
        # αναζητούμε πεδία που ξεκινούν με "account_"
        for key in form:
            if not key.startswith("account_"):
                continue
            # Skip account_g_cash (handle separately)
            if key == "account_g_cash":
                continue
            # key format: account_{vat}__{expense_tag}
            rest = key[len("account_"):]
            if "__" not in rest:
                continue
            vat_part, expense_tag = rest.split("__", 1)
            vat_key = vat_part.strip()
            code = form.get(key, "").strip()
            if vat_key not in accounts:
                accounts[vat_key] = {}
            accounts[vat_key][expense_tag] = code
        
        # αποθηκεύουμε global accounts
        save_global_accounts_to_credentials(accounts)
        
        # Handle account_g_cash separately (Γ-category specific)
        cash_acct = form.get("account_g_cash", "").strip()
        if cash_acct:
            settings = load_settings() or {}
            settings["account_g_cash"] = cash_acct
            save_settings(settings)
            log.info("save_accounts: saved account_g_cash=%s", cash_acct)
        
        flash("Οι γενικοί λογαριασμοί αποθηκεύτηκαν", "success")
    except Exception as e:
        log.exception("save_accounts failed")
        flash(f"Αδυναμία αποθήκευσης λογαριασμών: {e}", "error")
    return redirect(url_for("credentials"))

# ---------------- Save summary from modal to Excel & per-customer JSON ----------------

# --- BEGIN PATCH v3: /save_summary with repeat-entry also for receipts (per-line = "αποδειξακια") ---
# Paste this over your existing save_summary() in app.py

@app.route("/save_summary", methods=["POST"])
def save_summary():
    """
    Save summary and update epsilon:
      - Αν υπάρχει ήδη detailed εγγραφή στο epsilon για το mark -> update-only per-line
      - Αν υπάρχει μόνο placeholder -> σβήνουμε placeholders και γράφουμε Excel + append πλήρους εγγραφής
      - Αλλιώς -> γράφει Excel και προσθέτει πλήρη εγγραφή στο epsilon

    Επιπλέον:
      - Κόβει άκυρα/άδεια summaries (τέλος τα placeholders).
      - Εφαρμόζει **επαναληψιμη** (repeat-entry) κατηγοριοποίηση:
         * ΤΙΜΟΛΟΓΙΑ: category ανά γραμμή από mapping (VAT% -> κατηγορία).
         * ΑΠΟΔΕΙΞΕΙΣ: ΠΑΝΤΑ category ανά γραμμή = "αποδειξακια" (και top-level χαρακτηρισμός "αποδειξακια").
    """
    import os, json, re

    is_ajax_save = (
        request.is_json
        or (request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest")
        or (request.args.get("ajax") == "1")
        or (request.form.get("ajax") == "1")
    )

    def _save_summary_response(ok=True, message=None, error=None, status=200, **extra):
        if is_ajax_save:
            payload = {"ok": bool(ok)}
            if message:
                payload["message"] = message
            if error:
                payload["error"] = error
            if "saved_mark" not in extra:
                try:
                    saved_mark = str((summary or {}).get("mark") or (summary or {}).get("MARK") or "").strip()
                    if saved_mark:
                        payload["saved_mark"] = saved_mark
                except Exception:
                    pass
            if extra:
                payload.update(extra)
            return jsonify(payload), status
        return redirect(url_for("search"))

    _signal_remote_processing_start()

    @after_this_request
    def _clear_processing(response):
        _signal_remote_processing_end()
        return response

    try:
        from datetime import datetime as _dt, timezone as _tz
    except Exception:
        _dt = None
        _tz = None

    EXCEL_COLUMNS = [
        "MARK","ΑΦΜ","Επωνυμία","Σειρά","Αριθμός","Ημερομηνία","Είδος",
        "ΦΠΑ_ΚΑΤΗΓΟΡΙΑ","Καθαρή Αξία","ΦΠΑ","Σύνολο"
    ]

    # ---------- local helpers ----------
    def _first(*vals):
        for v in vals:
            if v is None:
                continue
            s = str(v).strip()
            if s and s.lower() not in ("none","nan"):
                return s
        return ""

    def _pfloat(x):
        try:
            return float(str(x).replace('.','').replace(',','.'))
        except Exception:
            return 0.0

    def _is_receipt(summary: dict) -> bool:
        s = summary or {}
        # explicit docType takes precedence over fuzzy heuristics
        dt = str(s.get("docType") or s.get("doc_type") or "").strip().lower()
        if dt.startswith("invoice"):
            return False
        if dt.startswith("receipt"):
            return True
        if s.get("is_receipt"):
            return True
        t  = str(s.get("type","")).strip()
        tn = str(s.get("type_name","")).lower()
        ch = str(s.get("χαρακτηρισμός") or s.get("characteristic") or s.get("category") or "")
        if ch == "αποδειξακια":
            return True
        if t in {"8.4","8.5","11.5"}:
            return True
        if any(h in tn for h in ("απόδειξη","apod","receipt","λιαν","pos")):
            return True
        if not s.get("totalVatAmount") and not s.get("totalNetValue") and s.get("totalValue"):
            return True
        return False

    def _receipt_analysis_enabled(summary: dict) -> bool:
        s = summary or {}
        return bool(
            s.get("receipt_analysis_enabled")
            or s.get("receipts_analysis_enabled")
            or s.get("receiptAnalysisEnabled")
            or s.get("analysis_receipts")
        )

    
    def _hydrate_summary_for_excel(summary: dict, vat: str = "") -> dict:
        try:
            s   = dict(summary or {})
            raw = s.get("raw") or {}
            m = _first(s.get("MARK"), s.get("mark")); s["MARK"] = m; s["mark"] = m
            aa = _first(s.get("number"), s.get("AA"), s.get("aa"), s.get("progressive_aa"),
                        raw.get("AA") if isinstance(raw, dict) else None,
                        raw.get("progressive_aa") if isinstance(raw, dict) else None)
            s["AA"] = aa; s["aa"] = aa
            if not s.get("number"): s["number"] = aa
            if not s.get("progressive_aa"): s["progressive_aa"] = aa
            s["AFM"]  = _first(s.get("AFM"), s.get("AFM_issuer"), s.get("issuer_vat"), vat)
            s["Name"] = _first(s.get("Name"), s.get("Name_issuer"), s.get("issuer_name"))
            s["issueDate"] = _first(s.get("issueDate"), s.get("issue_date"))
            tv = _first(s.get("totalValue"), s.get("total_value"), s.get("total_amount"))
            if not tv and isinstance(s.get("lines"), list):
                amt = sum(_pfloat(l.get("amount") or l.get("lineTotal") or l.get("total") or 0) for l in s["lines"])
                tv = f"{amt:.2f}"
            if tv: s["totalValue"] = str(tv)
            if _is_receipt(s):
                s["is_receipt"] = True
                if not _receipt_analysis_enabled(s):
                    s["category"] = s.get("category") or "αποδειξακια"
                    s["χαρακτηρισμός"] = s.get("χαρακτηρισμός") or s.get("characteristic") or "αποδειξακια"
                if not s.get("type_name"): s["type_name"] = "Απόδειξη"
            if not str(s.get("created_at") or "").strip():
                s["created_at"] = _dt.utcnow().isoformat(timespec="seconds") if _dt else ""
            return s
        except Exception:
            return summary or {}

    def _extract_vat_percent(line: dict) -> str:
        """
        Επιστρέφει κανονικοποιημένο κλειδί ποσοστού ΦΠΑ, π.χ. '24%' ή '0%'
        Διαβάζει από vatCategory ('ΦΠΑ 24%','24%') ή από vat ('24','24,00').
        """
        try:
            cand = _first(line.get("vatCategory"), line.get("vat_category"))
            if cand:
                m = re.search(r"(\d+)\s*%", str(cand))
                if m: return m.group(1) + "%"
                m = re.search(r"(\d+)", str(cand))
                if m: return m.group(1) + "%"
            cand2 = _first(line.get("vat"), line.get("vatRate"))
            if cand2:
                m = re.search(r"(\d+)", str(cand2).replace(',', '.'))
                if m: return m.group(1) + "%"
        except Exception:
            pass
        return ""

    def _load_repeat_entry_for_vat(vat: str):
        """Διαβάζει credentials.json και επιστρέφει repeat_entry για συγκεκριμένο ΑΦΜ."""
        try:
            creds_path = credentials_path_for_request()
            if not os.path.exists(creds_path):
                return {"enabled": False, "mapping": {}}
            with open(creds_path, "r", encoding="utf-8") as f:
                arr = json.load(f) or []
            v = str(vat or "").strip()
            for c in arr:
                try:
                    if str(c.get("vat") or c.get("AFM") or c.get("tax_number") or "").strip() == v:
                        out = c.get("repeat_entry") or {}
                        return {
                            "enabled": bool(out.get("enabled")),
                            "mapping": out.get("mapping") or {},
                            "invoice_mtype": out.get("invoice_mtype") or "",
                            "receipt_mtype": out.get("receipt_mtype") or ""
                        }
                except Exception:
                    continue
        except Exception:
            pass
        return {"enabled": False, "mapping": {}, "invoice_mtype": "", "receipt_mtype": ""}

    def _normalize_book_category(value) -> str:
        s = str(value or "").strip().upper()
        if s in ("Γ", "G"):
            return "G"
        if s in ("Β", "B"):
            return "B"
        return s

    def _resolve_mtype_fields(s: dict):
        s = s or {}
        settings = load_settings() or {}
        code_to_label = {
            str(settings.get("article_movement_type_agoron_exodon_tameiaki", "") or "").strip(): "Αγορών - Εξόδων Ταμειακή",
            str(settings.get("article_movement_type_tameiaki", "") or "").strip(): "Ταμειακή",
            str(settings.get("article_movement_type_agoron_exodon", "") or "").strip(): "Αγορών - Εξόδων Επί Πιστώσει",
            str(settings.get("article_movement_type_symsifistiki", "") or "").strip(): "Συμψηφιστική",
            str(settings.get("article_movement_type_agoron_exodon_opseos", "") or "").strip(): "Αγορών - Εξόδων Όψεως",
        }
        code_to_label = {k: v for k, v in code_to_label.items() if k}

        def _norm_label(txt: str) -> str:
            raw = str(txt or "").strip().lower()
            if not raw:
                return ""
            try:
                import unicodedata
                raw = unicodedata.normalize("NFD", raw)
                raw = "".join(ch for ch in raw if unicodedata.category(ch) != "Mn")
            except Exception:
                pass
            raw = raw.replace("—", " ").replace("–", " ").replace("-", " ").replace("_", " ")
            raw = re.sub(r"\s+", " ", raw).strip()
            return raw

        mtype_code = _first(
            s.get("mtype"),
            s.get("invoice_mtype"),
            s.get("receipt_mtype"),
            s.get("mType"),
            s.get("invoiceMtype"),
            s.get("receiptMtype"),
        )

        if not mtype_code:
            raw_label = str(s.get("mtype_label") or s.get("movement_type_label") or "").strip().lower()
            if raw_label:
                for code_key, label_val in code_to_label.items():
                    if raw_label == str(label_val).strip().lower():
                        mtype_code = code_key
                        break

                if not mtype_code:
                    raw_norm = _norm_label(raw_label)
                    cash_code = str(settings.get("article_movement_type_agoron_exodon_tameiaki") or settings.get("article_movement_type_tameiaki") or "").strip()
                    credit_code = str(settings.get("article_movement_type_agoron_exodon") or "").strip()
                    opseos_code = str(settings.get("article_movement_type_agoron_exodon_opseos") or "").strip()
                    syms_code = str(settings.get("article_movement_type_symsifistiki") or "").strip()

                    if "αγορ" in raw_norm and "εξοδ" in raw_norm and "ταμει" in raw_norm and cash_code:
                        mtype_code = cash_code
                    elif "ταμει" in raw_norm and cash_code:
                        mtype_code = cash_code
                    elif "συμψηφ" in raw_norm and syms_code:
                        mtype_code = syms_code
                    elif "οψε" in raw_norm and opseos_code:
                        mtype_code = opseos_code
                    elif "αγορ" in raw_norm and "εξοδ" in raw_norm and credit_code:
                        mtype_code = credit_code

        mtype_code = str(mtype_code or "").strip()
        mtype_label = code_to_label.get(mtype_code, "")
        if not mtype_label:
            mtype_label = str(s.get("mtype_label") or "").strip()

        return mtype_code, mtype_label

    def _should_create_cash_mirror(base_entry: dict) -> bool:
        try:
            if not isinstance(base_entry, dict):
                return False
            bcat = _normalize_book_category(base_entry.get("book_category"))
            if bcat != "G":
                return False

            settings = load_settings() or {}
            trigger_code = str(settings.get("article_movement_type_agoron_exodon_tameiaki", "") or "").strip()
            entry_code = str(base_entry.get("mtype") or "").strip()
            mtype_label_lower = str(base_entry.get("mtype_label") or "").lower()
            return bool((trigger_code and entry_code == trigger_code) or ("αγορών" in mtype_label_lower and "ταμει" in mtype_label_lower))
        except Exception:
            return False

    def _remove_cash_mirror_for_mark(epsilon_cache: list, mark: str) -> bool:
        try:
            removed = False
            target = str(mark or "").strip()
            for idx in range(len(epsilon_cache) - 1, -1, -1):
                row = epsilon_cache[idx]
                if str(row.get("mark") or "").strip() == target and bool(row.get("_auto_cash_payment")):
                    epsilon_cache.pop(idx)
                    removed = True
            if removed:
                log.info("save_summary: removed stale mirror cash-payment entries for MARK=%s", mark)
            return removed
        except Exception:
            log.exception("save_summary: failed removing stale mirror entries for MARK=%s", mark)
            return False

    def _maybe_append_cash_mirror(epsilon_cache: list, base_entry: dict, mark: str):
        try:
            if not isinstance(base_entry, dict):
                return False

            if not _should_create_cash_mirror(base_entry):
                return False

            settings = load_settings() or {}

            tameiaki_mtype = str(settings.get("article_movement_type_tameiaki", "14") or "").strip()
            cash_account = str(settings.get("account_g_cash", "") or "").strip()
            is_receipt = bool(base_entry.get("is_receipt"))
            supplier_account_key = "account_g_supplier_retail" if is_receipt else "account_g_supplier_wholesale"
            supplier_account = str(settings.get(supplier_account_key, "") or "").strip()
            if not (tameiaki_mtype and cash_account and supplier_account):
                return False

            # Avoid duplicate mirror entry for same MARK
            for item in (epsilon_cache or []):
                if str(item.get("mark") or "").strip() == str(mark or "").strip() and bool(item.get("_auto_cash_payment")):
                    return False

            from copy import deepcopy
            mirror_entry = deepcopy(base_entry)
            mirror_entry["mtype"] = tameiaki_mtype
            mirror_entry["mtype_label"] = "Ταμειακή"
            mirror_entry["_auto_cash_payment"] = True
            mirror_entry["book_category"] = "G"
            mirror_entry["is_receipt"] = is_receipt

            supplier_desc = "Χρέωση - Προμηθευτής λιανικής (auto)" if is_receipt else "Χρέωση - Προμηθευτής χονδρικής (auto)"
            supplier_category = "προμηθευτής_λιανικής" if is_receipt else "προμηθευτής_χονδρικής"
            amt_str = str(base_entry.get("totalValue") or "0")
            mirror_entry["lines"] = [
                {
                    "id": "mirror_debit",
                    "description": supplier_desc,
                    "amount": amt_str,
                    "vat": "0",
                    "category": supplier_category,
                    "vat_category": ""
                },
                {
                    "id": "mirror_credit",
                    "description": "Πίστωση - Ταμείο (auto)",
                    "amount": amt_str,
                    "vat": "0",
                    "category": "ταμείο",
                    "vat_category": ""
                }
            ]

            epsilon_cache.append(mirror_entry)
            log.info("save_summary: created mirror cash-payment entry MARK=%s with mtype=%s", mark, tameiaki_mtype)
            return True
        except Exception:
            log.exception("save_summary: mirror entry creation failed (non-blocking)")
            return False

    # ---------------- parse payload ----------------
    try:
        log.info("save_summary: start request from %s", request.remote_addr)
        try:
            # Debug: log incoming form keys and a snippet of raw body to diagnose missing mtype
            form_keys = list(request.form.keys()) if request.form else []
            log.debug("save_summary: request.form keys=%s, content-type=%s", form_keys, request.headers.get('Content-Type'))
            try:
                raw_snip = request.get_data(as_text=True) or ''
                if raw_snip:
                    log.debug("save_summary: raw request data (snippet, 2000 chars): %s", raw_snip[:2000])
            except Exception:
                log.debug("save_summary: could not read raw request data")
        except Exception:
            log.exception("save_summary: debug logging failed")
        raw = None
        # Prefer legacy form field `summary_json`, but also accept component `summary_data`
        if request.form and request.form.get("summary_json"):
            raw = request.form.get("summary_json")
            summary = None
        elif request.form and request.form.get("summary_data"):
            # component sometimes posts into `summary_data` (SummaryModal hidden input)
            raw = request.form.get("summary_data")
            summary = None
        else:
            if request.is_json:
                summary = request.get_json(force=True)
                raw = None
            else:
                raw = request.get_data(as_text=True) or None
                summary = None

        if raw:
            try:
                summary = json.loads(raw)
                log.debug("save_summary: parsed JSON summary keys: %s", list(summary.keys()))
            except Exception:
                log.warning("save_summary: failed json.loads(payload) - building minimal from form")
                summary = None

        if summary is None:
            summary = {}
            if request.form.get("mark"):
                summary["mark"] = request.form.get("mark")
                summary["AFM"]  = request.form.get("vat") or request.form.get("AFM") or ""
            if request.form.get("issueDate"):
                summary["issueDate"] = request.form.get("issueDate")
            # server-side fallback: if form included individual mtype fields, copy them
            try:
                # possible form keys: mtype, receipt_mtype, invoice_mtype or camelCase variants
                form_m = request.form.get('mtype') or request.form.get('receipt_mtype') or request.form.get('invoice_mtype')
                form_m = form_m or request.form.get('receiptMtype') or request.form.get('invoiceMtype') or request.form.get('mType')
                if form_m:
                    summary['mtype'] = form_m
            except Exception:
                pass
    except Exception:
        log.exception("save_summary: cannot parse payload")
        if not is_ajax_save:
            flash("Μη έγκυρα δεδομένα περίληψης", "error")
        return _save_summary_response(ok=False, error="Μη έγκυρα δεδομένα περίληψης", status=400)

    # Normalize possible camelCase keys produced by the component UI so the
    # server consistently finds `receipt_mtype` / `invoice_mtype` / `mtype`.
    try:
        if summary.get('receiptMtype') and not summary.get('receipt_mtype'):
            summary['receipt_mtype'] = summary.get('receiptMtype')
        if summary.get('invoiceMtype') and not summary.get('invoice_mtype'):
            summary['invoice_mtype'] = summary.get('invoiceMtype')
        if summary.get('mType') and not summary.get('mtype'):
            summary['mtype'] = summary.get('mType')
    except Exception:
        pass

    log.info("save_summary: received summary with keys: %s, mtype: %s, receipt_mtype: %s, invoice_mtype: %s", list(summary.keys()), summary.get("mtype", "NO MTYPE"), summary.get('receipt_mtype',''), summary.get('invoice_mtype',''))

    # ---------------- active VAT ----------------
    active = get_active_credential_from_session()
    vat = (active.get("vat") if active else None) or summary.get("AFM") or summary.get("AFM_issuer") or summary.get("AFM")
    if not vat:
        log.error("save_summary: missing vat - active=%s summary_afm=%s", bool(active), summary.get("AFM"))
        if not is_ajax_save:
            if not session.get('active_group'):
                flash("Δεν έχει επιλεγεί ενεργή ομάδα. Επίλεξε πρώτα ομάδα.", "error")
            else:
                flash("Δεν έχει επιλεγεί ενεργός πελάτης (ΑΦΜ)", "error")
        if not session.get('active_group'):
            return _save_summary_response(ok=False, error="Δεν έχει επιλεγεί ενεργή ομάδα. Επίλεξε πρώτα ομάδα.", status=400)
        return _save_summary_response(ok=False, error="Δεν έχει επιλεγεί ενεργός πελάτης (ΑΦΜ)", status=400)
    
    # If active credential is not set (or missing book_category), try to load from vat
    if not active or not active.get("book_category"):
        active = get_cred_by_vat(vat) or {}

    # ---------------- ensure lines / normalize ----------------
    def float_from_comma(value):
        if value is None: return 0.0
        if isinstance(value,(int,float)): return float(value)
        s = str(value).strip().replace(".","").replace(",",".")
        try: return float(s)
        except Exception: return 0.0

    lines = summary.get("lines", []) or []
    log.info("save_summary: received %d lines from request, first line mtype: %s", 
             len(lines), lines[0].get("mtype") if lines and isinstance(lines[0], dict) else "N/A")
    if not lines:
        try:
            mark = str(summary.get("mark","")).strip()
            docs_file = group_path(f"{vat}_invoices.json")
            all_docs = json_read(docs_file) or []
            docs_for_mark = [d for d in all_docs if str(d.get("mark","")).strip() == mark]
            reconstructed = []
            for idx, inst in enumerate(docs_for_mark):
                ln_id = inst.get("id") or inst.get("line_id") or inst.get("LineId") or f"{mark}_inst{idx}"
                description = _first(inst.get("description"), inst.get("desc"), inst.get("Description"), inst.get("Name"), inst.get("Name_issuer"), f"Instance #{idx+1}")
                amount = _first(inst.get("amount"), inst.get("lineTotal"), inst.get("totalNetValue"), inst.get("totalValue"))
                vat_rate = _first(inst.get("vat"), inst.get("vatRate"), inst.get("vatPercent"), inst.get("totalVatAmount"))
                raw_vatcat = _first(inst.get("vatCategory"), inst.get("vat_category"), inst.get("VATCategory"), inst.get("vatCat"), inst.get("vatCategoryCode"), inst.get("vat_code"))
                vat_cat_mapped = VAT_MAP.get(str(raw_vatcat).strip(), raw_vatcat) if raw_vatcat else ""
                reconstructed.append({"id": ln_id,"description": description,"amount": amount,"vat": vat_rate,"category": "","vatCategory": vat_cat_mapped})
            lines = reconstructed
            log.debug("save_summary: reconstructed %d lines from %s", len(lines), docs_file)
        except Exception:
            log.exception("save_summary: failed to reconstruct lines")
            lines = []

    normalized_lines = []
    for idx, ln in enumerate(lines):
        if not isinstance(ln, dict):
            continue
        ln_id = ln.get("id") or f"{summary.get('mark','')}_l{idx}"
        raw_vcat = _first(ln.get("vatCategory"), ln.get("vat_category"), ln.get("vatCat"), ln.get("vat_cat"))
        vcat_mapped = VAT_MAP.get(str(raw_vcat).strip(), raw_vcat) if raw_vcat else ""
        normalized_lines.append({
            "id": ln_id,
            "description": _first(ln.get("description"), ln.get("desc")),
            "amount": _first(ln.get("amount"), ln.get("lineTotal")),
            "vat": _first(ln.get("vat"), ln.get("vatRate")),
            "category": ln.get("category","") or "",
            "vatCategory": vcat_mapped
        })
    summary["lines"] = normalized_lines

    # recalc header totals for receipt analysis
    try:
        if normalized_lines and _is_receipt(summary) and _receipt_analysis_enabled(summary):
            total_net = 0.0
            total_vat = 0.0
            for ln in normalized_lines:
                try:
                    total_net += float_from_comma(ln.get("amount") or 0)
                except Exception:
                    pass
                try:
                    total_vat += float_from_comma(ln.get("vat") or 0)
                except Exception:
                    pass
            summary["totalNetValue"] = f"{total_net:.2f}"
            summary["totalVatAmount"] = f"{total_vat:.2f}"
            summary["totalValue"] = f"{(total_net + total_vat):.2f}"
    except Exception:
        log.exception("save_summary: failed recomputing header totals for receipt analysis")

    # ---------------- HYDRATE & repeat-entry ----------------
    summary = _hydrate_summary_for_excel(summary, vat=str(vat or ""))
    is_receipt = _is_receipt(summary)
    receipt_analysis_enabled = _receipt_analysis_enabled(summary)

    # If the client sent `receipt_mtype` or `invoice_mtype` explicitly, prefer that
    # over applying repeat-entry fallbacks later. This guards against clients
    # (fast-flow, components) that may populate the legacy hidden input instead
    # of the generic `mtype` field.
    if not summary.get('mtype'):
        if summary.get('receipt_mtype'):
            summary['mtype'] = summary.get('receipt_mtype')
        elif summary.get('invoice_mtype'):
            summary['mtype'] = summary.get('invoice_mtype')

    try:
        resolved_mtype_code, resolved_mtype_label = _resolve_mtype_fields(summary)
        if resolved_mtype_code:
            summary["mtype"] = resolved_mtype_code
        if resolved_mtype_label and not summary.get("mtype_label"):
            summary["mtype_label"] = resolved_mtype_label
    except Exception:
        log.exception("save_summary: mtype resolve failed")

    series_cred = _resolve_series_credential(summary, vat=vat)
    summary["series"] = _resolved_series_for_summary(summary, vat=vat, cred=series_cred)

    conf = _load_repeat_entry_for_vat(vat)
    payment_method_type = str(
        _first(
            summary.get("paymentMethodType"),
            summary.get("payment_method_type"),
            summary.get("paymentMethod"),
            summary.get("payment_method"),
        )
        or ""
    ).strip()

    try:
        is_g_invoice = (not is_receipt) and (_normalize_book_category(active.get("book_category")) == "G")
        if is_g_invoice and payment_method_type == "3":
            settings_payment = load_settings() or {}
            cash_mtype_code = str(
                settings_payment.get("article_movement_type_agoron_exodon_tameiaki")
                or settings_payment.get("article_movement_type_tameiaki")
                or ""
            ).strip()
            selected_mtype = str(summary.get("mtype") or summary.get("invoice_mtype") or "").strip()
            repeat_profile_mtype = str(conf.get("invoice_mtype") or "").strip() if conf.get("enabled") else ""
            effective_mtype = selected_mtype or repeat_profile_mtype

            if cash_mtype_code:
                if effective_mtype and effective_mtype != cash_mtype_code:
                    log.info(
                        "save_summary: G-category cash payment mismatch (selected='%s', profile='%s', expected='%s') -> preserving user/profile selection",
                        selected_mtype,
                        repeat_profile_mtype,
                        cash_mtype_code,
                    )
                # ΜΟΝΟ ενημερωτικό check: δεν κάνουμε forced override εδώ.
                # Το UI warning αποφασίζει αν θα αλλάξει σε cash MTYPE ή θα συνεχίσει όπως επέλεξε ο χρήστης.
    except Exception:
        log.exception("save_summary: payment/mtype enforcement failed")

    # ΤΙΜΟΛΟΓΙΑ: mapping ανά VAT%
    if not is_receipt:
        if conf.get("enabled") and isinstance(conf.get("mapping"), dict) and summary.get("lines"):
            mapping = conf["mapping"]
            for ln in summary["lines"]:
                if not ln: 
                    continue
                if str(ln.get("category","")).strip():
                    continue  # μην πατήσεις την επιλογή του χρήστη
                key = ""
                # normalized VAT key (π.χ. "24%")
                cand = _extract_vat_percent(ln)
                if cand:
                    key = cand
                if key in mapping:
                    ln["category"] = mapping[key]
                else:
                    key2 = key.replace('%','')
                    if key2 in mapping:
                        ln["category"] = mapping[key2]
            
            # Προσθήκη invoice-level MTYPE από repeat_entry (για Γ Κατηγορία)
            if conf.get("invoice_mtype") and not summary.get("mtype"):
                summary["mtype"] = conf["invoice_mtype"]
                log.info("save_summary: Applied invoice_mtype='%s' from repeat_entry", conf["invoice_mtype"])
    else:
        # ΑΠΟΔΕΙΞΕΙΣ: όταν ΔΕΝ υπάρχει ανάλυση, fallback ανά γραμμή "αποδειξακια" (αν λείπει)
        if (not receipt_analysis_enabled) and summary.get("lines"):
            for ln in summary["lines"]:
                if ln is None: 
                    continue
                if not str(ln.get("category","")).strip():
                    ln["category"] = "αποδειξακια"
        
        # Προσθήκη MTYPE από repeat_entry για αποδείξεις (χρήση receipt_mtype αντί invoice_mtype)
        if conf.get("enabled") and conf.get("receipt_mtype") and not summary.get("mtype"):
            summary["mtype"] = conf["receipt_mtype"]
            log.info("save_summary (receipt): Applied receipt_mtype='%s' from repeat_entry", conf["receipt_mtype"])
        elif conf.get("enabled") and conf.get("invoice_mtype") and not summary.get("mtype"):
            # Fallback σε invoice_mtype αν δεν υπάρχει receipt_mtype (για backward compatibility)
            summary["mtype"] = conf["invoice_mtype"]
            log.info("save_summary (receipt): Applied invoice_mtype='%s' from repeat_entry (fallback)", conf["invoice_mtype"])

    # Safety fallback: για Γ-category τιμολόγια, μην αφήνεις ποτέ κενό invoice mtype
    try:
        if (not is_receipt) and _normalize_book_category(active.get("book_category")) == "G" and not str(summary.get("mtype") or "").strip():
            settings_for_fallback = load_settings() or {}
            summary["mtype"] = str(settings_for_fallback.get("article_movement_type_agoron_exodon") or "12").strip()
            log.info("save_summary: Applied fallback invoice mtype='%s' for Γ-category invoice", summary.get("mtype"))
    except Exception:
        log.exception("save_summary: failed applying fallback invoice mtype")

    # Re-resolve mtype code/label after repeat-entry and fallbacks
    try:
        resolved_mtype_code, resolved_mtype_label = _resolve_mtype_fields(summary)
        summary["mtype"] = str(resolved_mtype_code or "").strip()
        if resolved_mtype_label:
            summary["mtype_label"] = resolved_mtype_label
    except Exception:
        log.exception("save_summary: mtype re-resolve failed after repeat-entry")

    # Persist repeat receipts preferences to backend (toggle + receipt_mtype) for mixed flow stability
    try:
        selected_receipt_mtype = str(
            summary.get("receipt_mtype")
            or summary.get("mtype")
            or ""
        ).strip()
        if is_receipt and conf.get("enabled") and selected_receipt_mtype:
            _sync_repeat_entry_backend(vat, enabled=True, receipt_mtype=selected_receipt_mtype)
            conf["receipt_mtype"] = selected_receipt_mtype
    except Exception:
        log.exception("save_summary: failed to persist receipt repeat prefs for VAT=%s", vat)

    # --- GUARD: μπλοκάρουμε άδεια/άκυρα summaries ---
    try:
        if not _meaningful_summary(summary):
            log.info("save_summary: blocked empty/invalid summary (no 15-digit MARK or no meaningful lines/totals)")
            if is_ajax_save:
                return jsonify({"ok": False, "error": "Empty or invalid summary"}), 400
            try:
                flash("Δεν υπάρχουν δεδομένα για αποθήκευση.", "error")
            except Exception:
                pass
            return _save_summary_response(ok=False, error="Δεν υπάρχουν δεδομένα για αποθήκευση.", status=400)
    except Exception:
        log.exception("save_summary: validation failed")
        if is_ajax_save:
            return jsonify({"ok": False, "error": "Validation error"}), 400
        return _save_summary_response(ok=False, error="Validation error", status=400)

    # ---------------- best-effort append στο per-customer JSON ----------------
    try:
        append_summary_to_customer_file(summary, vat)
    except Exception:
        log.exception("save_summary: append_summary_to_customer_file failed")

    # ---------------- epsilon cache (placeholders/detailed) ----------------
    def epsilon_item_has_detail(item):
        try:
            if not item or not isinstance(item, dict): return False
            if item.get("issueDate"): return True
            if item.get("totalNetValue") or item.get("totalValue"): return True
            lines_local = item.get("lines") or []
            if isinstance(lines_local, list):
                for l in lines_local:
                    if l and (l.get("description") or l.get("amount") or l.get("vat")):
                        return True
            if item.get("AFM_issuer") or item.get("AFM"):
                if item.get("aa") or item.get("AA") or item.get("issueDate") or item.get("totalValue"):
                    return True
        except Exception:
            pass
        return False

    try:
        epsilon_cache = load_epsilon_cache_for_vat(vat) or []
    except Exception:
        log.exception("save_summary: failed loading epsilon cache")
        epsilon_cache = []

    mark = str(summary.get("mark","")).strip()
    existing_index = None
    placeholder_indices = []
    try:
        for i, d in enumerate(epsilon_cache):
            d_mark = _first(d.get("mark"), d.get("MARK"), d.get("invoice_id"), d.get("Αριθμός Μητρώου"), d.get("id"))
            if not d_mark or d_mark != mark: 
                continue
            # For fallback receipt MARK, only match/update same actual receipt identity.
            if is_receipt and mark == RECEIPT_FALLBACK_MARK and not _same_receipt_identity(summary, d):
                continue
            if not epsilon_item_has_detail(d): 
                placeholder_indices.append(i); 
                continue
            existing_index = i; 
            break
    except Exception:
        log.exception("save_summary: scanning epsilon_cache failed")

    if placeholder_indices:
        try:
            for idx in sorted(placeholder_indices, reverse=True):
                try: epsilon_cache.pop(idx)
                except Exception: log.exception("save_summary: pop placeholder idx=%s", idx)
            try: _safe_save_epsilon_cache(vat, epsilon_cache)
            except Exception: log.exception("save_summary: persist after placeholder removal failed")
            existing_index = None
        except Exception:
            log.exception("save_summary: error removing placeholders")

    # ---------------- update-only per-line on existing detailed ----------------
    if existing_index is not None:
        try:
            existing = epsilon_cache[existing_index]
            existing_lines = existing.get("lines", []) or []
            by_id = {str(l.get("id","")): l for l in existing_lines if l.get("id") is not None}
            updated = False
            for ln in summary["lines"]:
                lid = str(ln.get("id","")); 
                if not lid: 
                    continue
                if lid in by_id:
                    el = by_id[lid]
                    new_cat = ln.get("category","") or ""
                    # Για αποδείξεις, αν λείπει στο υπάρχον -> γράψε "αποδειξακια"
                    if not new_cat and is_receipt:
                        new_cat = "αποδειξακια"
                    if new_cat and str(el.get("category","")) != new_cat:
                        el["category"] = new_cat; updated = True
                    if ln.get("vatCategory") and str(el.get("vat_category","")) != ln.get("vatCategory"):
                        el["vat_category"] = ln.get("vatCategory"); updated = True
                else:
                    existing_lines.append({
                        "id": lid, "description": ln.get("description",""),
                        "amount": ln.get("amount",""), "vat": ln.get("vat",""),
                        "category": (ln.get("category","") or ("αποδειξακια" if is_receipt else "")),
                        "vat_category": ln.get("vatCategory","") or ""
                    }); updated = True

            # Ενημέρωση MTYPE στο top-level του παραστατικού (αν υπάρχει στο summary)
            new_mtype, new_mtype_label = _resolve_mtype_fields(summary)
            if new_mtype and str(existing.get("mtype","")) != new_mtype:
                existing["mtype"] = new_mtype
                updated = True
            if new_mtype_label and str(existing.get("mtype_label", "")) != new_mtype_label:
                existing["mtype_label"] = new_mtype_label
                updated = True

            new_book_category = "G" if _normalize_book_category(active.get("book_category")) == "G" else ""
            if new_book_category and str(existing.get("book_category", "")) != new_book_category:
                existing["book_category"] = new_book_category
                updated = True
            if bool(existing.get("is_receipt")) != bool(is_receipt):
                existing["is_receipt"] = bool(is_receipt)
                updated = True
            if bool(existing.get("receipt_analysis_enabled")) != bool(receipt_analysis_enabled):
                existing["receipt_analysis_enabled"] = bool(receipt_analysis_enabled)
                updated = True

            should_have_mirror = _should_create_cash_mirror(existing)
            mirror_created = False
            mirror_removed = False
            if should_have_mirror:
                mirror_created = _maybe_append_cash_mirror(epsilon_cache, existing, mark)
            else:
                mirror_removed = _remove_cash_mirror_for_mark(epsilon_cache, mark)
                if mirror_removed:
                    updated = True
            
            if updated or mirror_created or mirror_removed:
                try:
                    if _dt and _tz: 
                        existing["_updated_at"] = _dt.now(_tz.utc).isoformat()
                except Exception:
                    existing["_updated_at"] = existing.get("_updated_at","")
                existing["lines"] = existing_lines
                epsilon_cache[existing_index] = existing
                try:
                    _safe_save_epsilon_cache(vat, epsilon_cache)
                except Exception:
                    log.exception("save_summary: failed saving epsilon cache after update")

                # ensure excel exists (αν λείπει, φτιάξ'το με σωστό Είδος/Τύπος)
                try:
                    excel_path = excel_path_for(vat=vat)
                    if not os.path.exists(excel_path):
                        tn = "ΑΠΟΔΕΙΞΗ" if is_receipt else _first(summary.get("type_name"), summary.get("type"))
                        total_net = float_from_comma(summary.get("totalNetValue", existing.get("totalNetValue","") or 0))
                        total_vat = float_from_comma(summary.get("totalVatAmount", existing.get("totalVatAmount","") or 0))
                        total_value = total_net + total_vat
                        # Για Β κατηγορία πελατών, το ΑΦΜ στο excel πρέπει να είναι "1"
                        book_category = str(active.get("book_category") or "Β").strip().upper()
                        afm_excel = "1" if book_category == "Β" else (summary.get("AFM_issuer") or summary.get("AFM") or vat)
                        row = {
                            "MARK": str(summary.get("mark", existing.get("mark",""))),
                            "ΑΦΜ": afm_excel,
                            "Επωνυμία": summary.get("Name", existing.get("Name_issuer","") or ""),
                            "Σειρά": summary.get("series", existing.get("series","") or ""),
                            "Αριθμός": summary.get("number", existing.get("AA", existing.get("aa",""))),
                            "Ημερομηνία": summary.get("issueDate", existing.get("issueDate","")),
                            "Είδος": tn,
                            "ΦΠΑ_ΚΑΤΗΓΟΡΙΑ": summary.get("vatCategory", existing.get("vatCategory","") or ""),
                            "Καθαρή Αξία": f"{total_net:.2f}".replace(".",","),
                            "ΦΠΑ": f"{total_vat:.2f}".replace(".",","),
                            "Σύνολο": f"{total_value:.2f}".replace(".",",")
                        }
                        import pandas as pd
                        os.makedirs(os.path.dirname(excel_path) or ".", exist_ok=True)
                        pd.DataFrame([row]).astype(str).fillna("").reindex(columns=EXCEL_COLUMNS, fill_value="").to_excel(excel_path, index=False, engine="openpyxl")
                except Exception:
                    log.exception("save_summary: ensure/create excel failed")

                if mirror_created:
                    msg = "Ενημερώθηκε το epsilon και δημιουργήθηκε ταμειακή εγγραφή."
                elif mirror_removed:
                    msg = "Ενημερώθηκε το epsilon και αφαιρέθηκε παλιά ταμειακή εγγραφή."
                else:
                    msg = "Ενημερώθηκε ο χαρακτηρισμός στο cache (epsilon)."
                if not is_ajax_save:
                    flash(msg, "success")
                return _save_summary_response(ok=True, message=msg)
            else:
                msg = "Δεν υπήρξε αλλαγή στις κατηγορίες."
                if not is_ajax_save:
                    flash(msg, "info")
                return _save_summary_response(ok=True, message=msg)
        except Exception:
            log.exception("save_summary: error processing existing detailed epsilon entry")
            if not is_ajax_save:
                flash("Σφάλμα διακομιστή κατά την επεξεργασία ενημέρωσης", "error")
            return _save_summary_response(ok=False, error="Σφάλμα διακομιστή κατά την επεξεργασία ενημέρωσης", status=500)

    # ---------------- create/write excel + append epsilon entry ----------------
    excel_path = excel_path_for(vat=vat)
    excel_written = False
    try:
        total_net = float_from_comma(summary.get("totalNetValue", 0))
        total_vat = float_from_comma(summary.get("totalVatAmount", 0))
        total_value = float_from_comma(summary.get("totalValue", total_net + total_vat))
        tipo_excel = "ΑΠΟΔΕΙΞΗ" if is_receipt else _first(summary.get("type_name"), summary.get("type"))

        # Για Β κατηγορία πελατών, το ΑΦΜ στο excel πρέπει να είναι "1"
        book_category = str(active.get("book_category") or "Β").strip().upper()
        afm_excel = "1" if book_category == "Β" else (summary.get("AFM_issuer") or summary.get("AFM") or vat)

        row = {
            "MARK": str(summary.get("mark","")),
            "ΑΦΜ": afm_excel,
            "Επωνυμία": summary.get("Name",""),
            "Σειρά": summary.get("series",""),
            "Αριθμός": summary.get("number",""),
            "Ημερομηνία": summary.get("issueDate",""),
            "Είδος": tipo_excel,
            "ΦΠΑ_ΚΑΤΗΓΟΡΙΑ": summary.get("vatCategory",""),
            "Καθαρή Αξία": f"{total_net:.2f}".replace(".",","),
            "ΦΠΑ": f"{total_vat:.2f}".replace(".",","),
            "Σύνολο": f"{total_value:.2f}".replace(".",",")
        }

        import pandas as pd
        df_new = pd.DataFrame([row]).astype(str).fillna("")
        df_new = df_new.reindex(columns=EXCEL_COLUMNS, fill_value="")

        helper = globals().get("_ensure_excel_and_update_or_append") or globals().get("_append_to_excel")
        if helper and callable(helper):
            try:
                log.debug("save_summary: calling helper %s for excel update", helper.__name__)
                try:
                    helper(summary, vat=vat)  # προτίμησε ολόκληρο το summary
                except TypeError:
                    helper(row, vat=vat)
                excel_written = True
            except Exception:
                log.exception("save_summary: helper %s failed; falling back to inline Excel write", helper.__name__)

        if not excel_written:
            if os.path.exists(excel_path):
                try:
                    df_existing = pd.read_excel(excel_path, engine="openpyxl", dtype=str).fillna("")
                    df_existing = df_existing.astype(str).fillna("")
                    cols = list(df_existing.columns)

                    aa_val = _first(summary.get("number"), summary.get("AA"), summary.get("aa"), summary.get("progressive_aa"))

                    row_full = {
                        "MARK": str(summary.get("mark","")),
                        "ΑΦΜ": summary.get("AFM_issuer") or summary.get("AFM") or vat,
                        "Επωνυμία": summary.get("Name",""),
                        "Σειρά": summary.get("series",""),
                        "Ημερομηνία": summary.get("issueDate",""),
                        "ΦΠΑ_ΚΑΤΗΓΟΡΙΑ": summary.get("vatCategory",""),
                        "Καθαρή Αξία": f"{total_net:.2f}".replace(".",","),
                        "ΦΠΑ": f"{total_vat:.2f}".replace(".",","),
                        "Σύνολο": f"{total_value:.2f}".replace(".",","),
                    }
                    if "Αριθμός" in cols: row_full["Αριθμός"] = aa_val
                    if "Α/Α"     in cols: row_full["Α/Α"]   = aa_val
                    if "Είδος"   in cols: row_full["Είδος"] = tipo_excel
                    if "Τύπος"   in cols and is_receipt: row_full["Τύπος"] = "ΑΠΟΔΕΙΞΗ"

                    row_aligned = {c: row_full.get(c, "") for c in cols}
                    pd.concat([df_existing, pd.DataFrame([row_aligned], columns=cols)], ignore_index=True, sort=False).to_excel(excel_path, index=False, engine="openpyxl")
                    flash("Αποθηκεύτηκε στο Excel.", "success")
                except Exception:
                    log.exception("save_summary: inline append to excel failed")
                    raise
            else:
                try:
                    os.makedirs(os.path.dirname(excel_path) or ".", exist_ok=True)
                    df_new.to_excel(excel_path, index=False, engine="openpyxl")
                    flash("Αποθηκεύτηκε στο Excel.", "success")
                except Exception:
                    log.exception("save_summary: create/write new excel failed")
                    raise
    except Exception:
        log.exception("save_summary: Excel write failed")
        flash("Αποτυχία αποθήκευσης Excel", "error")

    # ---------------- Build epsilon entry & append ----------------
    try:
        # Resolve mtype code/label robustly from summary payload
        mtype_code, mtype_label = _resolve_mtype_fields(summary)
        
        epsilon_entry = {
            "mark": mark,
            "issueDate": summary.get("issueDate",""),
            "series": summary.get("series",""),
            "aa": _first(summary.get("number"), summary.get("AA"), summary.get("aa")),
            "AA": _first(summary.get("number"), summary.get("AA"), summary.get("aa")),
            "type": summary.get("type",""),
            "vatCategory": summary.get("vatCategory",""),
            "totalNetValue": summary.get("totalNetValue",""),
            "totalVatAmount": summary.get("totalVatAmount",""),
            "totalValue": summary.get("totalValue",""),
            "classification": summary.get("classification",""),
            "category": (
                (summary.get("category") or "") if (is_receipt and receipt_analysis_enabled)
                else ("αποδειξακια" if is_receipt else (summary.get("category") or ""))
            ),
            "χαρακτηρισμός": (
                (summary.get("χαρακτηρισμός") or summary.get("characteristic") or "") if (is_receipt and receipt_analysis_enabled)
                else (summary.get("χαρακτηρισμός") or summary.get("characteristic") or ("αποδειξακια" if is_receipt else ""))
            ),
            "characteristic": (
                (summary.get("χαρακτηρισμός") or summary.get("characteristic") or "") if (is_receipt and receipt_analysis_enabled)
                else (summary.get("χαρακτηρισμός") or summary.get("characteristic") or ("αποδειξακια" if is_receipt else ""))
            ),
            "mtype": mtype_code,  # Είδος Κίνησης code για Γ Category (invoice-level)
            "mtype_label": mtype_label,  # Είδος Κίνησης label (για mirror detection)
            "book_category": "G" if _normalize_book_category(active.get("book_category")) == "G" else "",  # Γ-category flag
            "is_receipt": is_receipt,  # Flag: True για αποδείξεις, False για τιμολόγια
            "receipt_analysis_enabled": bool(receipt_analysis_enabled),
            "AFM_issuer": summary.get("AFM_issuer","") or summary.get("AFM",""),
            "Name_issuer": summary.get("Name_issuer") or summary.get("Name",""),
            "AFM": summary.get("AFM","") or vat,
            "lines": []
        }
        for ln in summary["lines"]:
            epsilon_entry["lines"].append({
                "id": ln.get("id",""),
                "description": ln.get("description",""),
                "amount": ln.get("amount",""),
                "vat": ln.get("vat",""),
                "category": (
                    ln.get("category","")
                    or ("" if (is_receipt and receipt_analysis_enabled) else ("αποδειξακια" if is_receipt else ""))
                ),
                "vat_category": ln.get("vatCategory","") or ""
            })

        # skip append αν δεν υπάρχει 15ψήφιο mark ή καθόλου ουσιαστικές lines.
        # Για αποδείξεις (ιδίως mixed mode), επιτρέπουμε append και με header-only
        # στοιχεία όταν υπάρχουν βασικά πεδία (AA/date/issuer/totals).
        _mk_ok = bool(re.fullmatch(r"\d{15}", str(mark or "").strip()))
        _has_lines = bool(summary["lines"]) and any(
            (str(x.get("description") or x.get("amount") or x.get("vat") or "").strip())
            for x in summary["lines"]
        )
        _receipt_header_ok = False
        if is_receipt:
            _receipt_header_ok = (
                str(
                    _first(
                        summary.get("AA"),
                        summary.get("aa"),
                        summary.get("number"),
                        summary.get("progressive_aa"),
                        summary.get("issueDate"),
                        summary.get("AFM_issuer"),
                        summary.get("AFM"),
                        summary.get("totalValue"),
                        summary.get("totalNetValue"),
                        summary.get("totalVatAmount"),
                    )
                    or ""
                ).strip()
                != ""
            )

        _has_payload_for_append = _has_lines or (is_receipt and _receipt_header_ok)
        if not _mk_ok or not _has_payload_for_append:
            log.info(
                "save_summary: skip epsilon append due to empty payload (mk_ok=%s, has_lines=%s, receipt_header_ok=%s, is_receipt=%s)",
                _mk_ok, _has_lines, _receipt_header_ok, is_receipt
            )
            return _save_summary_response(ok=True, message="Δεν υπήρχαν επαρκή δεδομένα για νέα εγγραφή epsilon.")

        try:
            epsilon_cache.append(epsilon_entry)
            _maybe_append_cash_mirror(epsilon_cache, epsilon_entry, mark)
            
            _safe_save_epsilon_cache(vat, epsilon_cache)
            msg = "Η περίληψη αποθηκεύτηκε και προστέθηκε νέα εγγραφή epsilon."
            if not is_ajax_save:
                flash(msg, "success")
            try:
                from utils import log_user_activity
                from flask_login import current_user
                from auth import get_active_group
                grp = get_active_group()
                log_user_activity(
                    user_id=getattr(current_user, 'id', None) or getattr(current_user, 'pw_hash', None),
                    group_name=grp.name if grp else 'unknown',
                    action='save_invoice',
                    details={
                        'vat': vat,
                        'mark': mark,
                        'AA': _first(summary.get("number"), summary.get("AA"), summary.get("aa"), summary.get("progressive_aa")),
                        'description': f'Αποθήκευση παραστατικού mark={mark}'
                    },
                    user_email=getattr(current_user, 'email', None),
                    user_username=getattr(current_user, 'username', None)
                )
            except Exception:
                log.exception("save_summary: failed to log activity")
        except Exception:
            log.exception("save_summary: failed saving new epsilon cache")
            if not is_ajax_save:
                flash("Αποτυχία ενημέρωσης cache epsilon (δείτε τα logs του διακομιστή)", "error")
            return _save_summary_response(ok=False, error="Αποτυχία ενημέρωσης cache epsilon", status=500)
    except Exception:
        log.exception("save_summary: failed building/appending epsilon entry")
        if not is_ajax_save:
            flash("Αποτυχία ενημέρωσης cache epsilon", "error")
        return _save_summary_response(ok=False, error="Αποτυχία ενημέρωσης cache epsilon", status=500)

    return _save_summary_response(ok=True, message=msg if 'msg' in locals() else "Η αποθήκευση ολοκληρώθηκε.")
# --- END PATCH v3 ---





@app.route("/save_receipt", methods=["POST"])
def save_receipt():
    _signal_remote_processing_start()

    @after_this_request
    def _clear_processing(response):
        _signal_remote_processing_end()
        return response

    receipt = request.form.to_dict()
    vat = receipt.get("issuer_vat")
    year = receipt.get("issue_date")[-4:]
    category = receipt.get("category")
    mtype = receipt.get("mtype", "")  # Είδος Κίνησης για Γ Κατηγορία

    # --- Αποθήκευση Excel ---
    excel_file = f"uploads/{vat}_{year}_invoices.xlsx"
    df_new = pd.DataFrame([receipt])
    if os.path.exists(excel_file):
        df_existing = pd.read_excel(excel_file)
        df_combined = pd.concat([df_existing, df_new], ignore_index=True)
    else:
        df_combined = df_new
    df_combined.to_excel(excel_file, index=False)

    # --- Αποθήκευση JSON ---
    json_file = f"data/{vat}_epsilon_invoices.json"
    if os.path.exists(json_file):
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = []

    receipt["category"] = category  # αποθηκεύουμε τον server-side χαρακτηρισμό
    # Αποθηκεύουμε το mtype στο root level του invoice (αν υπάρχει)
    if mtype:
        receipt["mtype"] = mtype
    data.append(receipt)
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    # Log save_receipt activity for live-update detection
    try:
        from utils import log_user_activity
        from flask_login import current_user
        from auth import get_active_group
        grp = get_active_group()
        try:
            log_user_activity(
                user_id=getattr(current_user, 'id', None) or getattr(current_user, 'pw_hash', None),
                group_name=grp.name if grp else 'unknown',
                action='save_receipt',
                details={
                    'vat': vat,
                    'file': os.path.basename(excel_file) if excel_file else None,
                    'description': f'Αποθήκευση απόδειξης για ΑΦΜ {vat}'
                },
                user_email=getattr(current_user, 'email', None),
                user_username=getattr(current_user, 'username', None)
            )
        except Exception:
            log.exception("save_receipt: failed to call log_user_activity")
    except Exception:
        try:
            current_app.logger.exception("save_receipt: failed to import or log activity")
        except Exception:
            pass

    return redirect(url_for("search"))

@app.route("/api/repeat_state/get", methods=["GET"])
def api_repeat_state_get():
    try:
        active = get_active_credential_from_session() or {}
        vat = active.get("vat") or ""
        enabled = _repeat_state_get_enabled_for_vat(vat)
        return jsonify({"ok": True, "enabled": bool(enabled), "vat": vat})
    except Exception as e:
        log.exception("repeat_state/get failed")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/repeat_state/set", methods=["POST"])
def api_repeat_state_set():
    """
    Body: { "enabled": true/false }
    Προαιρετικά μπορείς να στείλεις και { "mapping": {...} } – θα αγνοηθεί εδώ,
    κρατάμε μόνο το enabled ώστε να είναι ultra-stable για receipts autosave.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        enabled = bool(payload.get("enabled", False))

        active = get_active_credential_from_session() or {}
        vat = str(payload.get("vat") or active.get("vat") or "").strip()

        # ενημέρωσε session
        try:
            session["repeat_enabled"] = enabled
        except Exception:
            pass

        # ενημέρωσε repeat_state.json (per VAT)
        data = _repeat_state_load()
        if vat not in data:
            data[vat] = {}
        data[vat]["enabled"] = enabled
        _repeat_state_save(data)

        # Sync και στο credentials repeat_entry.enabled για σταθερό reload state
        try:
            _sync_repeat_entry_backend(vat, enabled=enabled)
        except Exception:
            log.exception("repeat_state/set: credentials sync failed for VAT=%s", vat)

        return jsonify({"ok": True, "enabled": enabled, "vat": vat})
    except Exception as e:
        log.exception("repeat_state/set failed")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/confirm_receipt", methods=["POST"])
def api_confirm_receipt():
    """
    Επιβεβαίωση/Αποθήκευση ΑΠΟΔΕΙΞΗΣ:
      - Normalizes summary σε μορφή 'ΑΠΟΔΕΙΞΗ' + 1 γραμμή αν δεν υπάρχουν lines.
    - Θέτει category 'αποδειξακια' ανά γραμμή (αν λείπει), μόνο όταν δεν είναι ενεργή ανάλυση αποδείξεων.
      - Idempotent ενημέρωση epsilon cache (update-by-mark).
      - Γράφει/ενημερώνει Excel (μία φορά).
    Επιστρέφει JSON: { ok, saved, mark, excel_written, updated_existing }
    """
    _signal_remote_processing_start()

    @after_this_request
    def _clear_processing(response):
        _signal_remote_processing_end()
        return response

    try:
        payload = request.get_json(force=True, silent=True) or {}
    except Exception:
        payload = {}
    summary = payload.get("summary") or payload

    def _receipt_analysis_enabled(summary_obj: dict) -> bool:
        s = summary_obj or {}
        return bool(
            s.get("receipt_analysis_enabled")
            or s.get("receipts_analysis_enabled")
            or s.get("receiptAnalysisEnabled")
            or s.get("analysis_receipts")
        )

    # --- unify/normalize receipt shape ---
    def _norm_receipt(s: dict) -> dict:
        s = dict(s or {})
        analysis_enabled = _receipt_analysis_enabled(s)
        s["type"] = s.get("type") or "ΑΠΟΔΕΙΞΗ"
        s["type_name"] = s.get("type_name") or "ΑΠΟΔΕΙΞΗ"
        s["is_receipt"] = True
        # κύρια πεδία
        s["mark"] = str(s.get("mark") or s.get("MARK") or "").strip()
        s["AA"] = str(s.get("AA") or s.get("aa") or s.get("number") or "").strip()
        s["issueDate"] = str(s.get("issueDate") or s.get("date") or "").strip()
        s["AFM_issuer"] = s.get("AFM_issuer") or s.get("AFM") or ""
        s["AFM"] = s.get("AFM") or s.get("AFM_issuer") or ""
        # γραμμές
        lines = s.get("lines") or []
        if not lines:
            amt = str(s.get("totalValue") or s.get("total_amount") or s.get("totalNetValue") or "").strip()
            if amt:
                lines = [{
                    "id": "r0", "description": "",
                    "amount": amt, "vat": "",
                    "category": s.get("category") or "",
                    "vat_category": s.get("vatCategory") or ""
                }]
        # εξασφάλισε 'αποδειξακια' ανά γραμμή
        fixed = []
        for idx, ln in enumerate(lines):
            if not isinstance(ln, dict):
                continue
            cat = (ln.get("category") or "").strip()
            if not cat and not analysis_enabled:
                cat = "αποδειξακια"
            fixed.append({
                "id": ln.get("id") or f"r{idx}",
                "description": ln.get("description") or "",
                "amount": ln.get("amount") or ln.get("total") or "",
                "vat": ln.get("vat") or ln.get("vatRate") or "",
                "category": cat,
                "vat_category": ln.get("vat_category") or ln.get("vatCategory") or ""
            })
        s["lines"] = fixed

        inferred_line_category = ""
        if analysis_enabled:
            for ln in fixed:
                if not isinstance(ln, dict):
                    continue
                c = str(ln.get("category") or "").strip()
                if c:
                    inferred_line_category = c
                    break

        # totals (συμπλήρωσε αν λείπουν)
        if not str(s.get("totalValue") or "").strip():
            try:
                tot = sum(float(str(x.get("amount") or "0").replace(".", "").replace(",", ".")) for x in fixed)
                s["totalValue"] = f"{tot:.2f}".replace(".", ",")
            except Exception:
                pass
        # set top-level receipt category
        if not analysis_enabled:
            s["category"] = s.get("category") or "αποδειξακια"
            s["χαρακτηρισμός"] = s.get("χαρακτηρισμός") or s.get("characteristic") or "αποδειξακια"
        else:
            s["category"] = s.get("category") or inferred_line_category or ""
            s["χαρακτηρισμός"] = s.get("χαρακτηρισμός") or s.get("characteristic") or inferred_line_category or ""
        s["characteristic"] = s["χαρακτηρισμός"]
        return s

    summary = _norm_receipt(summary)
    receipt_analysis_enabled = _receipt_analysis_enabled(summary)

    # --- guard: απαιτούμε τουλάχιστον mark 15ψήφιο ή “ουσιαστικές” γραμμές/σύνολο ---
    if not _meaningful_summary(summary):
        log.info("api_confirm_receipt: not meaningful payload -> 400")
        return jsonify({"ok": False, "error": "Empty or invalid receipt"}), 400

    # active VAT
    active = get_active_credential_from_session()
    vat = (active.get("vat") if active else None) or summary.get("AFM") or summary.get("AFM_issuer")
    if not vat:
        return jsonify({"ok": False, "error": "No active VAT"}), 400

    # Resolve custom series settings for receipt flows as well (including analysis mode)
    try:
        series_cred = active if isinstance(active, dict) else _resolve_series_credential(summary, vat=vat)
        summary["series"] = _resolved_series_for_summary(summary, vat=vat, cred=series_cred)
    except Exception:
        log.exception("api_confirm_receipt: failed resolving series")

    # Ανάκτηση MTYPE από repeat_entry (αν είναι Γ Κατηγορία)
    invoice_mtype = summary.get("mtype", "")
    if not invoice_mtype and active:
        try:
            repeat_entry = active.get("repeat_entry") or {}
            invoice_mtype = (
                repeat_entry.get("receipt_mtype", "")
                or repeat_entry.get("invoice_mtype", "")
            )
        except Exception:
            pass

    book_category = str((active or {}).get("book_category") or "").strip().upper()
    is_g_category = book_category in ("Γ", "G")

    def _should_create_cash_mirror_local(base_entry: dict) -> bool:
        try:
            if not isinstance(base_entry, dict):
                return False
            if str(base_entry.get("book_category") or "").strip().upper() != "G":
                return False
            settings_local = load_settings() or {}
            trigger_code = str(settings_local.get("article_movement_type_agoron_exodon_tameiaki", "") or "").strip()
            entry_code = str(base_entry.get("mtype") or "").strip()
            mtype_label_lower = str(base_entry.get("mtype_label") or "").lower()
            return bool((trigger_code and entry_code == trigger_code) or ("αγορών" in mtype_label_lower and "ταμει" in mtype_label_lower))
        except Exception:
            return False

    def _remove_cash_mirror_for_mark_local(epsilon_cache_local: list, mark_local: str) -> bool:
        try:
            removed = False
            target = str(mark_local or "").strip()
            for idx_local in range(len(epsilon_cache_local) - 1, -1, -1):
                row_local = epsilon_cache_local[idx_local]
                if str(row_local.get("mark") or "").strip() == target and bool(row_local.get("_auto_cash_payment")):
                    epsilon_cache_local.pop(idx_local)
                    removed = True
            return removed
        except Exception:
            log.exception("api_confirm_receipt: failed removing mirror entries for MARK=%s", mark_local)
            return False

    def _maybe_append_cash_mirror_local(epsilon_cache_local: list, base_entry: dict, mark_local: str) -> bool:
        try:
            if not isinstance(base_entry, dict):
                return False
            if not _should_create_cash_mirror_local(base_entry):
                return False

            settings_local = load_settings() or {}
            tameiaki_mtype = str(settings_local.get("article_movement_type_tameiaki", "14") or "").strip()
            cash_account = str(settings_local.get("account_g_cash", "") or "").strip()
            is_receipt_local = bool(base_entry.get("is_receipt"))
            supplier_key = "account_g_supplier_retail" if is_receipt_local else "account_g_supplier_wholesale"
            supplier_account = str(settings_local.get(supplier_key, "") or "").strip()
            if not (tameiaki_mtype and cash_account and supplier_account):
                return False

            for row_local in (epsilon_cache_local or []):
                if str(row_local.get("mark") or "").strip() == str(mark_local or "").strip() and bool(row_local.get("_auto_cash_payment")):
                    return False

            from copy import deepcopy
            mirror_entry = deepcopy(base_entry)
            mirror_entry["mtype"] = tameiaki_mtype
            mirror_entry["mtype_label"] = "Ταμειακή"
            mirror_entry["_auto_cash_payment"] = True
            mirror_entry["book_category"] = "G"
            mirror_entry["is_receipt"] = is_receipt_local

            supplier_desc = "Χρέωση - Προμηθευτής λιανικής (auto)" if is_receipt_local else "Χρέωση - Προμηθευτής χονδρικής (auto)"
            supplier_category = "προμηθευτής_λιανικής" if is_receipt_local else "προμηθευτής_χονδρικής"
            amount_str = str(base_entry.get("totalValue") or "0")
            mirror_entry["lines"] = [
                {
                    "id": "mirror_debit",
                    "description": supplier_desc,
                    "amount": amount_str,
                    "vat": "0",
                    "category": supplier_category,
                    "vat_category": ""
                },
                {
                    "id": "mirror_credit",
                    "description": "Πίστωση - Ταμείο (auto)",
                    "amount": amount_str,
                    "vat": "0",
                    "category": "ταμείο",
                    "vat_category": ""
                }
            ]
            epsilon_cache_local.append(mirror_entry)
            return True
        except Exception:
            log.exception("api_confirm_receipt: failed creating cash mirror for MARK=%s", mark_local)
            return False

    # φόρτωσε epsilon cache χωρίς auto-build
    try:
        epsilon_cache = load_epsilon_cache_for_vat(vat) or []
    except Exception:
        log.exception("api_confirm_receipt: load_epsilon_cache_for_vat failed")
        epsilon_cache = []

    mark = str(summary.get("mark") or "").strip()

    # index by mark (fallback receipt MARK requires same receipt identity)
    existing_idx = None
    for i, it in enumerate(epsilon_cache):
        try:
            m = str(it.get("mark") or it.get("MARK") or "").strip()
            if m == mark:
                if mark == RECEIPT_FALLBACK_MARK and not _same_receipt_identity(summary, it):
                    continue
                existing_idx = i
                break
        except Exception:
            continue

    updated_existing = False

    # ετοίμασε item προς αποθήκευση (receipt)
    epsilon_item = {
        "mark": mark,
        "issueDate": summary.get("issueDate", ""),
        "series": summary.get("series", ""),
        "aa": summary.get("AA", "") or summary.get("aa", "") or summary.get("number", ""),
        "AA": summary.get("AA", "") or summary.get("aa", "") or summary.get("number", ""),
        "type": "ΑΠΟΔΕΙΞΗ",
        "vatCategory": summary.get("vatCategory", ""),
        "totalNetValue": summary.get("totalNetValue", ""),
        "totalVatAmount": summary.get("totalVatAmount", ""),
        "totalValue": summary.get("totalValue", ""),
        "classification": summary.get("classification", ""),
        "category": (summary.get("category", "") if receipt_analysis_enabled else "αποδειξακια"),
        "χαρακτηρισμός": ((summary.get("χαρακτηρισμός") or summary.get("characteristic") or "") if receipt_analysis_enabled else "αποδειξακια"),
        "characteristic": ((summary.get("χαρακτηρισμός") or summary.get("characteristic") or "") if receipt_analysis_enabled else "αποδειξακια"),
        "AFM_issuer": summary.get("AFM_issuer", "") or summary.get("AFM", ""),
        "Name_issuer": summary.get("Name_issuer", "") or summary.get("Name", ""),
        "AFM": summary.get("AFM", "") or vat,
        "receipt_analysis_enabled": bool(receipt_analysis_enabled),
        "book_category": "G" if is_g_category else "",
        "is_receipt": True,
        "lines": [{
            "id": ln.get("id", ""),
            "description": ln.get("description", ""),
            "amount": ln.get("amount", ""),
            "vat": ln.get("vat", ""),
            "category": (ln.get("category") or ("" if receipt_analysis_enabled else "αποδειξακια")),
            "vat_category": ln.get("vat_category", "") or ""
        } for ln in (summary.get("lines") or [])]
    }
    # Προσθήκη MTYPE στο root level (μόνο για Γ Κατηγορία)
    if invoice_mtype:
        epsilon_item["mtype"] = invoice_mtype

    mirror_created = False
    mirror_removed = False

    # idempotent update
    if existing_idx is not None:
        existing = epsilon_cache[existing_idx]
        existing_lines = existing.get("lines")
        if not isinstance(existing_lines, list):
            existing_lines = []
            existing["lines"] = existing_lines

        by_id = {str(l.get("id", "")): l for l in existing_lines if isinstance(l, dict)}
        changed = False

        for ln in epsilon_item["lines"]:
            lid = str(ln.get("id") or "")
            if not lid:
                continue
            if lid in by_id:
                cur = by_id[lid]
                if (cur.get("category") or "") != (ln.get("category") or ""):
                    cur["category"] = ln["category"]
                    changed = True
                if (cur.get("vat_category") or "") != (ln.get("vat_category") or ""):
                    cur["vat_category"] = ln["vat_category"]
                    changed = True
            else:
                existing_lines.append(ln)
                changed = True

        if invoice_mtype and str(existing.get("mtype") or "").strip() != str(invoice_mtype).strip():
            existing["mtype"] = invoice_mtype
            changed = True
        if str(existing.get("book_category") or "").strip().upper() != ("G" if is_g_category else ""):
            existing["book_category"] = "G" if is_g_category else ""
            changed = True
        if bool(existing.get("is_receipt")) != True:
            existing["is_receipt"] = True
            changed = True
        if bool(existing.get("receipt_analysis_enabled")) != bool(receipt_analysis_enabled):
            existing["receipt_analysis_enabled"] = bool(receipt_analysis_enabled)
            changed = True

        if _should_create_cash_mirror_local(existing):
            mirror_created = _maybe_append_cash_mirror_local(epsilon_cache, existing, mark)
        else:
            mirror_removed = _remove_cash_mirror_for_mark_local(epsilon_cache, mark)
            if mirror_removed:
                changed = True

        if changed or mirror_created or mirror_removed:
            epsilon_cache[existing_idx] = existing
            updated_existing = True
    else:
        epsilon_cache.append(epsilon_item)
        mirror_created = _maybe_append_cash_mirror_local(epsilon_cache, epsilon_item, mark)

    # save epsilon atomically
    try:
        _safe_save_epsilon_cache(vat, epsilon_cache)
        try:
            from utils import log_user_activity
            from flask_login import current_user
            from auth import get_active_group
            grp = get_active_group()
            log_user_activity(
                user_id=getattr(current_user, 'id', None) or getattr(current_user, 'pw_hash', None),
                group_name=grp.name if grp else 'unknown',
                action='save_receipt',
                details={
                    'vat': vat,
                    'mark': mark,
                    'updated_existing': bool(updated_existing),
                    'description': f'Αποθήκευση απόδειξης/τιμολογίου mark={mark}'
                },
                user_email=getattr(current_user, 'email', None),
                user_username=getattr(current_user, 'username', None)
            )
        except Exception:
            log.exception("api_confirm_receipt: failed to log activity")
    except Exception:
        log.exception("api_confirm_receipt: _safe_save_epsilon_cache failed")

    # ensure excel line exists (single write)
    excel_written = False
    try:
        helper = globals().get("_ensure_excel_and_update_or_append") or globals().get("_append_to_excel")
        if helper and callable(helper):
            try:
                helper(summary, vat=vat)
                excel_written = True
            except Exception:
                log.exception("api_confirm_receipt: excel helper failed")
    except Exception:
        log.exception("api_confirm_receipt: excel write outer failed")

    return jsonify({
        "ok": True,
        "saved": True,
        "mark": mark,
        "excel_written": bool(excel_written),
        "updated_existing": bool(updated_existing)
    })











# ---------------- Help / Support Desk (Discord bridge) ----------------

# Simple SSE push subsystem for support chat.
# Clients can open an EventSource to /api/support/events to receive new-message
# notifications without polling.
_support_sse_lock = threading.Lock()
_support_sse_clients: List["queue.Queue[str]"] = []

import queue  # noqa: E402


def _support_sse_broadcast(payload: Dict[str, Any]):
    msg = json.dumps(payload)
    with _support_sse_lock:
        for q in list(_support_sse_clients):
            try:
                q.put_nowait(msg)
            except Exception:
                pass


def _support_store_path() -> str:
    return group_path("support_tickets.json")


def _support_upload_dir() -> str:
    p = group_path("support_uploads")
    os.makedirs(p, exist_ok=True)
    return p


def _support_load() -> Dict[str, Any]:
    p = _support_store_path()
    data = _safe_json_read(p, default={})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("tickets", [])
    data.setdefault("messages", [])
    data.setdefault("next_id", 1)
    data.setdefault("next_message_id", 1)
    return data


def _support_save(data: Dict[str, Any]) -> None:
    json_write(_support_store_path(), data or {})


def _support_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _support_get_my_open_ticket(data: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    for t in (data.get("tickets") or []):
        if str(t.get("user_id") or "") == str(user_id) and str(t.get("status") or "open") == "open":
            return t
    return None


def _support_slug(text: str, fallback: str = "user") -> str:
    raw = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(text or "").strip())
    raw = re.sub(r"-+", "-", raw).strip("-")
    return (raw or fallback)[:48]


def _support_close_ticket_record(ticket: Dict[str, Any], closed_by: str = "user") -> None:
    ticket["status"] = "closed"
    ticket["closed_at"] = _support_now_iso()
    ticket["closed_by"] = closed_by
    ticket["updated_at"] = _support_now_iso()


def _support_next_message_id(data: Dict[str, Any]) -> int:
    mid = int(data.get("next_message_id") or 1)
    data["next_message_id"] = mid + 1
    return mid


def _support_store_uploaded_files(files: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not files:
        return out
    root = _support_upload_dir()
    for f in files:
        if not getattr(f, "filename", None):
            continue
        original = secure_filename(f.filename or "")[:120]
        if not original:
            continue
        ext = os.path.splitext(original)[1][:10]
        token = secrets.token_hex(12)
        disk_name = f"{token}{ext}"
        disk_path = os.path.join(root, disk_name)
        f.save(disk_path)
        try:
            size = int(os.path.getsize(disk_path) or 0)
        except Exception:
            size = 0
        out.append({
            "id": token,
            "name": original,
            "size": size,
            "mime": str(getattr(f, "mimetype", "") or "application/octet-stream"),
            "path": disk_name,
            "url": f"/api/support/attachment/{token}",
        })
    return out


def _support_discord_archive_thread(thread_id: str) -> bool:
    token = (os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_KEY") or "").strip()
    if not token or not thread_id:
        return False
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.patch(
            f"https://discord.com/api/v10/channels/{thread_id}",
            headers=headers,
            json={"archived": True, "locked": True},
            timeout=12,
        )
        if not r.ok:
            current_app.logger.warning("Discord archive thread failed: %s %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception:
        current_app.logger.warning("Discord archive thread failed", exc_info=True)
        return False


def _support_discord_headers() -> Optional[Dict[str, str]]:
    token = (os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_KEY") or "").strip()
    if not token:
        return None
    return {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }


def _support_discord_channel_id() -> str:
    return (
        os.getenv("DISCORD_SUPPORT_CHANNEL_ID")
        or os.getenv("DISCORD_CHANNEL_ID")
        or "1471125053524414536"
    ).strip()


def _support_discord_log_event(event_text: str) -> bool:
    """Send lightweight lifecycle logs (open/close/readable events) in support channel."""
    headers = _support_discord_headers()
    channel_id = _support_discord_channel_id()
    if not headers or not channel_id:
        return False
    return _support_discord_send_message(channel_id, event_text, headers)


def _support_discord_send_message(channel_id: str, content: str, headers: Dict[str, str], attachments: Optional[List[Dict[str, Any]]] = None) -> bool:
    if not channel_id:
        return False
    content = (content or "")[:1800]
    files = []
    handles = []
    try:
        if attachments:
            payload = {"content": content}
            for i, a in enumerate(attachments[:5]):
                rel = str(a.get("path") or "")
                if not rel:
                    continue
                abs_path = os.path.join(_support_upload_dir(), rel)
                if not os.path.exists(abs_path):
                    continue
                h = open(abs_path, "rb")
                handles.append(h)
                files.append((f"files[{len(files)}]", (a.get("name") or f"file-{i}", h, a.get("mime") or "application/octet-stream")))
            post_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            r = requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=post_headers,
                data={"payload_json": json.dumps(payload)},
                files=files or None,
                timeout=20,
            )
        else:
            r = requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=headers,
                json={"content": content},
                timeout=12,
            )
        if not r.ok:
            current_app.logger.warning("Discord send message failed: %s %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception:
        current_app.logger.warning("Discord send message failed", exc_info=True)
        return False
    finally:
        for h in handles:
            try:
                h.close()
            except Exception:
                pass


def _support_discord_create_thread(ticket: Dict[str, Any], first_message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Optional[str]:
    headers = _support_discord_headers()
    channel_id = _support_discord_channel_id()
    if not headers or not channel_id:
        return None

    try:
        thread_name = f"ticket-{ticket['id']}-{_support_slug(ticket.get('display_name') or ticket.get('username') or 'user')}"
        th = requests.post(
            f"https://discord.com/api/v10/channels/{channel_id}/threads",
            headers=headers,
            json={
                "name": thread_name[:100],
                "auto_archive_duration": 1440,
                "type": 12,
                "invitable": False,
            },
            timeout=12,
        )
        if not th.ok:
            seed = requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=headers,
                json={"content": f"[Ticket #{ticket['id']}] Νέο αίτημα υποστήριξης από {ticket.get('display_name') or ticket.get('username') or 'user'}"},
                timeout=12,
            )
            if not seed.ok:
                current_app.logger.warning("Discord seed message failed: %s %s", seed.status_code, seed.text[:300])
                return None
            msg_id = (seed.json() or {}).get("id")
            if not msg_id:
                return None
            th = requests.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}/threads",
                headers=headers,
                json={
                    "name": thread_name[:100],
                    "auto_archive_duration": 1440,
                },
                timeout=12,
            )

        if not th.ok:
            current_app.logger.warning("Discord thread creation failed: %s %s", th.status_code, th.text[:300])
            return None
        thread_id = (th.json() or {}).get("id")
        if not thread_id:
            return None

        content = (
            f"[Ticket #{ticket['id']}]\n"
            f"User: {ticket.get('display_name') or ticket.get('username') or 'user'}\n"
            f"VAT: {ticket.get('vat') or '-'}\n"
            f"Group: {ticket.get('group_name') or '-'}\n"
            f"Message:\n{first_message}"
        )
        if not _support_discord_send_message(str(thread_id), content, headers, attachments=attachments):
            return None
        return str(thread_id)
    except Exception:
        current_app.logger.warning("Discord thread integration failed", exc_info=True)
        return None


def _support_discord_deliver_message(ticket: Dict[str, Any], message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, Optional[str]]:
    headers = _support_discord_headers()
    channel_id = _support_discord_channel_id()
    if not headers or not channel_id:
        return False, None

    thread_id = str(ticket.get("discord_thread_id") or "").strip()
    if not thread_id:
        thread_id = _support_discord_create_thread(ticket, message, attachments=attachments) or ""
        if thread_id:
            ticket["discord_thread_id"] = thread_id
            return True, thread_id

        fallback = (
            f"[Ticket #{ticket.get('id')}] από {ticket.get('display_name') or ticket.get('username') or 'user'}\n"
            f"VAT: {ticket.get('vat') or '-'}\n"
            f"Group: {ticket.get('group_name') or '-'}\n"
            f"{message}"
        )
        return _support_discord_send_message(channel_id, fallback, headers, attachments=attachments), None

    content = (
        f"[Ticket #{ticket.get('id')}] {ticket.get('display_name') or ticket.get('username') or 'user'}\n"
        f"{message}"
    )
    return _support_discord_send_message(thread_id, content, headers, attachments=attachments), thread_id


def _support_discord_sync_thread_messages(data: Dict[str, Any], ticket: Dict[str, Any], force: bool = False) -> bool:
    """Pull latest human replies from Discord thread into local ticket storage.

    Additionally, query thread metadata and if the Discord thread is archived/locked
    propagate that state locally (mark ticket closed) so the UI reflects closures
    performed on the Discord side even when no new reply is posted.
    """
    thread_id = str(ticket.get("discord_thread_id") or "").strip()
    headers = _support_discord_headers()
    if not thread_id or not headers:
        return False

    # lightweight rate-limiting for syncs
    if not force:
        last_sync = str(ticket.get("last_discord_sync_at") or "")
        if last_sync:
            try:
                dt = datetime.datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
                if (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() < 6:
                    return False
            except Exception:
                pass

    # First: check thread metadata to detect archived/locked state (mirror Discord-side close)
    try:
        meta_r = requests.get(f"https://discord.com/api/v10/channels/{thread_id}", headers=headers, timeout=8)
        if meta_r.ok:
            meta = meta_r.json() or {}
            # thread objects include an "archived" flag for threads
            if bool(meta.get("archived") or meta.get("locked")):
                if str(ticket.get("status") or "").lower() != "closed":
                    _support_close_ticket_record(ticket, closed_by="admin")
                    ticket["updated_at"] = _support_now_iso()
                    _support_save(data)
                    _support_discord_log_event(f"🔴 Ticket #{ticket.get('id')} έκλεισε (detected archived thread)")
                    # mark last sync so UI will pick this up immediately
                    ticket["last_discord_sync_at"] = _support_now_iso()
                    return True
    except Exception:
        # metadata check is best-effort; continue to attempt message sync
        current_app.logger.debug("Discord thread meta check failed", exc_info=True)

    existing = {
        str(m.get("discord_message_id") or "")
        for m in (data.get("messages") or [])
        if str(m.get("ticket_id") or "") == str(ticket.get("id") or "") and m.get("discord_message_id")
    }
    bot_user_id = str(os.getenv("DISCORD_BOT_USER_ID") or "").strip()

    try:
        r = requests.get(
            f"https://discord.com/api/v10/channels/{thread_id}/messages",
            headers=headers,
            params={"limit": 50},
            timeout=12,
        )
        if not r.ok:
            current_app.logger.warning("Discord sync failed: %s %s", r.status_code, r.text[:240])
            return False
        arr = r.json() or []
        if not isinstance(arr, list):
            return False

        changed = False
        support_replied = False
        for item in reversed(arr):
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or "")
            if not mid or mid in existing:
                continue
            author = item.get("author") or {}
            author_id = str(author.get("id") or "")
            if bot_user_id and author_id == bot_user_id:
                continue
            if bool(author.get("bot")):
                continue

            content = str(item.get("content") or "").strip()
            atts = []
            for a in (item.get("attachments") or []):
                if not isinstance(a, dict):
                    continue
                atts.append({
                    "id": str(a.get("id") or secrets.token_hex(8)),
                    "name": str(a.get("filename") or "attachment"),
                    "url": str(a.get("url") or "").strip(),
                    "mime": str(a.get("content_type") or "application/octet-stream"),
                    "size": int(a.get("size") or 0),
                    "path": None,
                })

            if not content and not atts:
                continue

            data["messages"].append({
                "id": _support_next_message_id(data),
                "ticket_id": int(ticket.get("id") or 0),
                "sender": "support",
                "content": content[:4000],
                "attachments": atts,
                "timestamp": str(item.get("timestamp") or _support_now_iso()),
                "read_by_user_at": None,
                "discord_message_id": mid,
                "source": "discord_sync",
            })
            existing.add(mid)
            changed = True
            support_replied = True

        if support_replied:
            now = _support_now_iso()
            for m in (data.get("messages") or []):
                if int(m.get("ticket_id") or 0) == int(ticket.get("id") or 0) and m.get("sender") == "user" and not m.get("read_by_support_at"):
                    m["read_by_support_at"] = now
            ticket["last_support_read_at"] = now

        if changed:
            ticket["updated_at"] = _support_now_iso()
        ticket["last_discord_sync_at"] = _support_now_iso()
        _support_save(data)
        return changed
    except Exception:
        current_app.logger.warning("Discord thread sync exception", exc_info=True)
        return False




def _support_discord_deliver_message(ticket: Dict[str, Any], message: str, attachments: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, Optional[str]]:
    headers = _support_discord_headers()
    channel_id = _support_discord_channel_id()
    if not headers or not channel_id:
        return False, None

    thread_id = str(ticket.get("discord_thread_id") or "").strip()
    if not thread_id:
        thread_id = _support_discord_create_thread(ticket, message, attachments=attachments) or ""
        if thread_id:
            ticket["discord_thread_id"] = thread_id
            return True, thread_id

        fallback = (
            f"[Ticket #{ticket.get('id')}] από {ticket.get('display_name') or ticket.get('username') or 'user'}\n"
            f"VAT: {ticket.get('vat') or '-'}\n"
            f"Group: {ticket.get('group_name') or '-'}\n"
            f"{message}"
        )
        return _support_discord_send_message(channel_id, fallback, headers, attachments=attachments), None

    content = (
        f"[Ticket #{ticket.get('id')}] {ticket.get('display_name') or ticket.get('username') or 'user'}\n"
        f"{message}"
    )
    return _support_discord_send_message(thread_id, content, headers, attachments=attachments), thread_id


@app.get("/api/support/events")
@login_required
def api_support_events():
    """Server-Sent Events stream for support chat updates."""
    def generator():
        q = queue.Queue()
        with _support_sse_lock:
            _support_sse_clients.append(q)
        try:
            while True:
                try:
                    msg = q.get(timeout=15)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    # keep-alive comment
                    yield ": keep-alive\n\n"
        finally:
            with _support_sse_lock:
                if q in _support_sse_clients:
                    _support_sse_clients.remove(q)

    return Response(stream_with_context(generator()), mimetype='text/event-stream')


@app.get("/api/support/attachment/<attachment_id>")
@login_required
def api_support_attachment(attachment_id: str):
    user_id = str(getattr(current_user, "id", "") or getattr(current_user, "pw_hash", ""))
    if not user_id:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = _support_load()
    my_ticket = _support_get_my_open_ticket(data, user_id)
    owned_ticket_ids = set()
    if my_ticket:
        owned_ticket_ids.add(int(my_ticket.get("id") or 0))
    for t in (data.get("tickets") or []):
        if str(t.get("user_id") or "") == user_id:
            owned_ticket_ids.add(int(t.get("id") or 0))

    target = None
    for m in (data.get("messages") or []):
        if int(m.get("ticket_id") or 0) not in owned_ticket_ids:
            continue
        for a in (m.get("attachments") or []):
            if str(a.get("id") or "") == str(attachment_id):
                target = a
                break
        if target:
            break
    if not target:
        return jsonify({"ok": False, "error": "not found"}), 404
    rel = str(target.get("path") or "")
    abs_path = os.path.join(_support_upload_dir(), rel)
    if not os.path.exists(abs_path):
        return jsonify({"ok": False, "error": "missing file"}), 404
    return send_file(abs_path, mimetype=target.get("mime") or "application/octet-stream", as_attachment=False, download_name=target.get("name") or "attachment")


@app.post("/api/support/ticket/open")
@login_required
def api_support_open_ticket():
    is_multipart = request.content_type and "multipart/form-data" in request.content_type
    payload = request.form if is_multipart else (request.get_json(silent=True) or {})
    message = str(payload.get("message") or "").strip()
    display_name = str(payload.get("display_name") or "").strip()
    files = request.files.getlist("files") if is_multipart else []

    if not message and not files:
        return jsonify({"ok": False, "error": "Το μήνυμα ή αρχείο είναι υποχρεωτικό."}), 400
    if not display_name:
        return jsonify({"ok": False, "error": "Δήλωσε όνομα πριν ξεκινήσεις συνομιλία."}), 400

    from auth import get_active_group
    grp = get_active_group()
    user_id = str(getattr(current_user, "id", "") or getattr(current_user, "pw_hash", ""))
    if not user_id:
        return jsonify({"ok": False, "error": "Μη έγκυρος χρήστης."}), 400

    data = _support_load()
    ticket = _support_get_my_open_ticket(data, user_id)
    was_new_ticket = False
    if not ticket:
        tid = int(data.get("next_id") or 1)
        data["next_id"] = tid + 1
        active = get_active_credential_from_session() or {}
        ticket = {
            "id": tid,
            "status": "open",
            "discord_thread_id": None,
            "user_id": user_id,
            "username": getattr(current_user, "username", None) or getattr(current_user, "email", None) or "user",
            "display_name": display_name[:120],
            "vat": str(active.get("vat") or ""),
            "group_name": getattr(grp, "name", None) if grp else None,
            "created_at": _support_now_iso(),
            "updated_at": _support_now_iso(),
            "support_typing_until": None,
            "last_support_read_at": None,
            "last_user_read_at": None,
        }
        data["tickets"].append(ticket)
        was_new_ticket = True
    else:
        ticket["display_name"] = display_name[:120]

    attachments = _support_store_uploaded_files(files)
    msg_obj = {
        "id": _support_next_message_id(data),
        "ticket_id": ticket["id"],
        "sender": "user",
        "content": message[:4000],
        "attachments": attachments,
        "timestamp": _support_now_iso(),
        "discord_delivered": False,
        "discord_delivered_at": None,
        "read_by_support_at": None,
        "read_by_user_at": None,
    }
    data["messages"].append(msg_obj)
    ticket["updated_at"] = _support_now_iso()

    delivered, _ = _support_discord_deliver_message(ticket, message or "(attachment)", attachments=attachments)
    msg_obj["discord_delivered"] = bool(delivered)
    if delivered:
        msg_obj["discord_delivered_at"] = _support_now_iso()

    _support_save(data)

    if was_new_ticket:
        who = ticket.get('display_name') or ticket.get('username') or 'user'
        _support_discord_log_event(
            f"🟢 Ticket #{ticket.get('id')} άνοιξε από {who} · VAT: {ticket.get('vat') or '-'} · Group: {ticket.get('group_name') or '-'}"
        )

    return jsonify({"ok": True, "ticket": ticket, "discord_delivered": bool(delivered), "message": msg_obj})


@app.get("/api/support/ticket/me")
@login_required
def api_support_my_ticket():
    user_id = str(getattr(current_user, "id", "") or getattr(current_user, "pw_hash", ""))
    mark_read = str(request.args.get("mark_read") or "0") in ("1", "true", "yes")
    data = _support_load()
    ticket = _support_get_my_open_ticket(data, user_id)
    if not ticket:
        user_tickets = [t for t in (data.get("tickets") or []) if str(t.get("user_id") or "") == user_id]
        if not user_tickets:
            return jsonify({"ok": True, "ticket": None, "messages": [], "presence": {"support_typing": False}, "archived_tickets": [], "closed_notice": False})

        user_tickets.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
        archived_tickets = []
        for t in user_tickets:
            if str(t.get("status") or "").lower() != "closed":
                continue
            archived_tickets.append({
                "id": t.get("id"),
                "display_name": t.get("display_name") or t.get("username") or "Χρήστης",
                "closed_at": t.get("closed_at"),
                "closed_by": t.get("closed_by"),
            })
            if len(archived_tickets) >= 20:
                break

        latest_closed = archived_tickets[0] if archived_tickets else None
        closed_notice = bool(latest_closed and str(latest_closed.get("closed_by") or "") in ("admin", "support", "admin/support"))
        return jsonify({
            "ok": True,
            "ticket": None,
            "messages": [],
            "presence": {"support_typing": False},
            "closed_notice": closed_notice,
            "archived_tickets": archived_tickets,
            "latest_closed_ticket": latest_closed,
        })

    # Pull direct Discord thread replies (admin/mod messages) even if webhook bridge did not post them.
    _support_discord_sync_thread_messages(data, ticket)

    msgs = [m for m in (data.get("messages") or []) if int(m.get("ticket_id") or 0) == int(ticket.get("id") or 0)]
    msgs.sort(key=lambda x: str(x.get("timestamp") or ""))

    changed = False
    if mark_read:
        now = _support_now_iso()
        for m in msgs:
            if m.get("sender") == "support" and not m.get("read_by_user_at"):
                m["read_by_user_at"] = now
                changed = True
        ticket["last_user_read_at"] = now
        changed = True

    typing_until_dt = _support_parse_iso_utc(ticket.get("support_typing_until"))
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    support_typing = bool(typing_until_dt and typing_until_dt > now_dt)

    if changed:
        _support_save(data)

    # Also include archived tickets metadata so the client can show the archive
    archived_tickets = []
    try:
        user_tickets = [t for t in (data.get("tickets") or []) if str(t.get("user_id") or "") == user_id]
        user_tickets.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
        for t in user_tickets:
            if str(t.get("status") or "").lower() != "closed":
                continue
            archived_tickets.append({
                "id": t.get("id"),
                "display_name": t.get("display_name") or t.get("username") or "Χρήστης",
                "closed_at": t.get("closed_at"),
                "closed_by": t.get("closed_by"),
            })
            if len(archived_tickets) >= 20:
                break
    except Exception:
        archived_tickets = []

    latest_closed = archived_tickets[0] if archived_tickets else None
    closed_notice = bool(latest_closed and str(latest_closed.get("closed_by") or "") in ("admin", "support", "admin/support"))

    return jsonify({
        "ok": True,
        "ticket": ticket,
        "messages": msgs[-100:],
        "presence": {"support_typing": support_typing},
        "archived_tickets": archived_tickets,
        "closed_notice": closed_notice,
        "latest_closed_ticket": latest_closed,
    })


@app.post("/api/support/ticket/close")
@login_required
def api_support_close_ticket():
    user_id = str(getattr(current_user, "id", "") or getattr(current_user, "pw_hash", ""))
    data = _support_load()
    ticket = _support_get_my_open_ticket(data, user_id)
    if not ticket:
        return jsonify({"ok": True, "closed": False, "message": "Δεν υπάρχει ανοικτό ticket."})

    thread_id = str(ticket.get("discord_thread_id") or "")
    ticket_id = ticket.get("id")
    who = ticket.get('display_name') or ticket.get('username') or 'user'
    _support_close_ticket_record(ticket, closed_by="user")
    _support_save(data)
    if thread_id:
        _support_discord_archive_thread(thread_id)
    _support_discord_log_event(f"🔴 Ticket #{ticket_id} έκλεισε από χρήστη: {who}")
    return jsonify({"ok": True, "closed": True, "ticket": ticket})


@app.get("/api/support/ticket/history/<int:ticket_id>")
@login_required
def api_support_ticket_history(ticket_id: int):
    user_id = str(getattr(current_user, "id", "") or getattr(current_user, "pw_hash", ""))
    data = _support_load()

    ticket = None
    for t in (data.get("tickets") or []):
        if int(t.get("id") or 0) == int(ticket_id) and str(t.get("user_id") or "") == user_id:
            ticket = t
            break

    if not ticket:
        return jsonify({"ok": False, "error": "ticket not found"}), 404

    msgs = [m for m in (data.get("messages") or []) if int(m.get("ticket_id") or 0) == int(ticket_id)]
    msgs.sort(key=lambda x: str(x.get("timestamp") or ""))
    return jsonify({"ok": True, "ticket": ticket, "messages": msgs[-200:]})


@app.post("/api/support/ticket/delete/<int:ticket_id>")
@login_required
def api_support_delete_ticket(ticket_id: int):
    """Permanently delete an archived (closed) ticket and its messages/attachments.
    Only the ticket owner may delete their closed tickets.
    """
    user_id = str(getattr(current_user, "id", "") or getattr(current_user, "pw_hash", ""))
    data = _support_load()

    # find ticket
    ticket = None
    for t in (data.get("tickets") or []):
        if int(t.get("id") or 0) == int(ticket_id) and str(t.get("user_id") or "") == user_id:
            ticket = t
            break

    if not ticket:
        return jsonify({"ok": False, "error": "ticket not found"}), 404

    if str(ticket.get("status") or "").lower() != "closed":
        return jsonify({"ok": False, "error": "only closed tickets can be deleted"}), 400

    # remove associated messages and attachments
    msgs = data.get("messages") or []
    remaining_msgs = []
    for m in msgs:
        if int(m.get("ticket_id") or 0) == int(ticket_id):
            # attempt to remove attachments from disk
            for a in (m.get("attachments") or []):
                rel = str(a.get("path") or "").strip()
                if rel:
                    try:
                        abs_path = os.path.join(_support_upload_dir(), rel)
                        if os.path.exists(abs_path):
                            os.remove(abs_path)
                    except Exception:
                        current_app.logger.debug("Failed to remove support attachment %s", rel, exc_info=True)
            continue
        remaining_msgs.append(m)

    data["messages"] = remaining_msgs

    # remove the ticket record
    data["tickets"] = [t for t in (data.get("tickets") or []) if int(t.get("id") or 0) != int(ticket_id)]

    _support_save(data)
    _support_discord_log_event(f"🗑️ Ticket #{ticket_id} διαγράφηκε από χρήστη")
    return jsonify({"ok": True, "deleted": True})


def _support_find_ticket_for_payload(data: Dict[str, Any], payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tickets = data.get("tickets") or []
    candidates = []

    nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    for key in ("ticket_id", "ticket", "id", "ticketId"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            candidates.append(("id", int(str(value).strip())))
        except Exception:
            pass
        try:
            nested_value = nested.get(key) if isinstance(nested, dict) else None
            if nested_value is not None:
                candidates.append(("id", int(str(nested_value).strip())))
        except Exception:
            pass

    for key in ("discord_thread_id", "thread_id", "channel_id", "threadId", "channelId"):
        value = str(payload.get(key) or "").strip()
        if value:
            candidates.append(("thread", value))
        nested_val = str(nested.get(key) or "").strip() if isinstance(nested, dict) else ""
        if nested_val:
            candidates.append(("thread", nested_val))

    for match_type, value in candidates:
        for t in tickets:
            if match_type == "id" and int(t.get("id") or 0) == int(value):
                return t
            if match_type == "thread" and str(t.get("discord_thread_id") or "").strip() == str(value):
                return t
    return None


def _support_parse_iso_utc(value: str) -> Optional[datetime.datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None



@app.post("/api/support/discord/reply")
def api_support_discord_reply():
    secret = (os.getenv("SUPPORT_WEBHOOK_SECRET") or "").strip()
    provided = (request.headers.get("X-Support-Secret") or "").strip()
    if secret and provided != secret:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "reply").strip().lower()
    content = str(payload.get("content") or "").strip()

    data = _support_load()
    ticket = _support_find_ticket_for_payload(data, payload)
    if ticket is None:
        return jsonify({"ok": False, "error": "ticket not found"}), 404

    thread_id = str(ticket.get("discord_thread_id") or payload.get("discord_thread_id") or payload.get("thread_id") or payload.get("channel_id") or "").strip()

    action_aliases = {
        "typing": "typing_start",
        "typing_on": "typing_start",
        "start_typing": "typing_start",
        "typing_end": "typing_stop",
        "stop_typing": "typing_stop",
        "typing_off": "typing_stop",
        "mark_read": "read",
        "seen": "read",
        "message_read": "read",
    }
    action = action_aliases.get(action, action)

    now = _support_now_iso()
    if action == "typing_start":
        ttl = max(5, min(45, int(payload.get("ttl_sec") or 15)))
        expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=ttl)
        ticket["support_typing_until"] = expires.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for m in (data.get("messages") or []):
            if int(m.get("ticket_id") or 0) == int(ticket.get("id") or 0) and m.get("sender") == "user" and not m.get("read_by_support_at"):
                m["read_by_support_at"] = now
        ticket["last_support_read_at"] = now
        _support_save(data)
        return jsonify({"ok": True, "typing": True})

    if action == "typing_stop":
        ticket["support_typing_until"] = None
        _support_save(data)
        return jsonify({"ok": True, "typing": False})

    if action == "read":
        for m in (data.get("messages") or []):
            if int(m.get("ticket_id") or 0) != int(ticket.get("id") or 0):
                continue
            if m.get("sender") == "user" and not m.get("read_by_support_at"):
                m["read_by_support_at"] = now
        ticket["last_support_read_at"] = now
        _support_save(data)
        return jsonify({"ok": True, "read": True})

    attachments = []
    for a in (payload.get("attachments") or []):
        if not isinstance(a, dict):
            continue
        attachments.append({
            "id": str(a.get("id") or secrets.token_hex(8)),
            "name": str(a.get("filename") or a.get("name") or "attachment"),
            "url": str(a.get("url") or "").strip(),
            "mime": str(a.get("content_type") or a.get("mime") or "application/octet-stream"),
            "size": int(a.get("size") or 0),
            "path": None,
        })

    if action == "close":
        if content or attachments:
            data["messages"].append({
                "id": _support_next_message_id(data),
                "ticket_id": int(ticket.get("id") or 0),
                "sender": "support",
                "content": content[:4000],
                "attachments": attachments,
                "timestamp": now,
                "read_by_user_at": None,
            })
        _support_close_ticket_record(ticket, closed_by="admin")
        _support_save(data)
        if thread_id:
            _support_discord_archive_thread(thread_id)
        _support_discord_log_event(f"🔴 Ticket #{ticket.get('id')} έκλεισε από admin/support")
        return jsonify({"ok": True, "closed": True})

    if action == "reply" and not content and not attachments:
        return jsonify({"ok": False, "error": "missing content"}), 400

    data["messages"].append({
        "id": _support_next_message_id(data),
        "ticket_id": int(ticket.get("id") or 0),
        "sender": "support",
        "content": content[:4000],
        "attachments": attachments,
        "timestamp": now,
        "read_by_user_at": None,
    })
    ticket["support_typing_until"] = None
    ticket["updated_at"] = now

    for m in (data.get("messages") or []):
        if int(m.get("ticket_id") or 0) == int(ticket.get("id") or 0) and m.get("sender") == "user" and not m.get("read_by_support_at"):
            m["read_by_support_at"] = now
    ticket["last_support_read_at"] = now

    _support_save(data)

    # Notify SSE listeners (see /api/support/events) that a new support message arrived
    try:
        _support_sse_broadcast({
            "type": "support_reply",
            "ticket_id": int(ticket.get("id") or 0),
            "content": content[:4000],
        })
    except Exception:
        pass

    return jsonify({"ok": True})


def _normalize_backup_member(name: str) -> str:
    name = (name or "").replace("\\", "/")
    name = name.lstrip("/")
    parts = [p for p in name.split("/") if p not in ("", ".", "..")]
    if parts and parts[0].lower() == "data":
        parts = parts[1:]
    return os.path.join(*parts) if parts else ""


def _human_readable_size(num: int) -> str:
    try:
        value = float(num)
    except Exception:
        return str(num)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


def _open_backup_zip(source, password: Optional[bytes] = None):
    """Open a zip (plain or AES-encrypted) and return a context manager.
    Tries plain zipfile first (old unencrypted backups); on failure tries
    pyzipper with the server-derived AES key (new encrypted backups)."""
    if password is None:
        try:
            password = _derive_backup_password().encode('utf-8')
        except Exception:
            password = b'scanmydata_bkup'

    # 1) Try plain stdlib ZipFile — works for unencrypted backups
    try:
        zf = zipfile.ZipFile(source)
        # Probe: peek at the first member; AES-encrypted members raise RuntimeError
        for name in zf.namelist():
            try:
                with zf.open(name) as f:
                    f.read(4)
                break
            except RuntimeError as probe_err:
                if 'password' in str(probe_err).lower() or 'encrypt' in str(probe_err).lower():
                    zf.close()
                    raise
                break
            except Exception:
                break
        zf.close()
        try:
            source.seek(0)
        except Exception:
            pass
        return zipfile.ZipFile(source)
    except (RuntimeError, Exception):
        pass

    # 2) Fallback: AES-encrypted via pyzipper
    try:
        import pyzipper
        try:
            source.seek(0)
        except Exception:
            pass
        zf = pyzipper.AESZipFile(source)
        zf.setpassword(password)
        return zf
    except Exception as e:
        raise zipfile.BadZipFile(f"Δεν ήταν δυνατό το άνοιγμα του ZIP (plain ή AES): {e}")


def _analyze_backup_zip(file_like) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "file_count": 0,
        "total_size": 0,
        "total_size_human": "0 B",
        "customers_total": None,
        "vat_total": 0,
        "sample_vats": [],
        "contains_credentials": False,
        "contains_settings": False,
        "mode": "group",
        "selected_vats": [],
        "modified_first": None,
        "modified_last": None,
    }
    try:
        file_like.seek(0)
    except Exception:
        pass

    with _open_backup_zip(file_like) as zf:
        infos = [info for info in zf.infolist() if not info.is_dir()]
        summary["file_count"] = len(infos)
        summary["total_size"] = sum(info.file_size for info in infos)
        summary["total_size_human"] = _human_readable_size(summary["total_size"])

        timestamps: List[datetime.datetime] = []
        vats: set[str] = set()
        customer_count: Optional[int] = None

        for info in infos:
            try:
                timestamps.append(datetime.datetime(*info.date_time))
            except Exception:
                pass

            rel = _normalize_backup_member(info.filename)
            if not rel:
                continue

            if rel == "credentials.json":
                summary["contains_credentials"] = True
                try:
                    with zf.open(info) as fh:
                        data = json.loads(fh.read().decode("utf-8-sig"))
                    if isinstance(data, list):
                        customer_count = len(data)
                        for item in data:
                            if isinstance(item, dict):
                                vat_val = item.get("vat") or item.get("VAT") or item.get("afm") or item.get("AFM")
                                if vat_val:
                                    vats.add(str(vat_val).strip())
                    elif isinstance(data, dict):
                        customer_count = len(data.keys())
                except Exception:
                    current_app.logger.warning("Failed to parse credentials.json from backup", exc_info=True)

            if rel == "credentials_settings.json":
                summary["contains_settings"] = True

            if rel == "backup_manifest.json":
                try:
                    with zf.open(info) as fh:
                        manifest = json.loads(fh.read().decode("utf-8-sig"))
                    if isinstance(manifest, dict):
                        mode = str(manifest.get("mode") or "").strip().lower()
                        if mode in {"group", "customer"}:
                            summary["mode"] = mode
                        manifest_vats = manifest.get("selected_vats")
                        if isinstance(manifest_vats, list):
                            summary["selected_vats"] = [str(v).strip() for v in manifest_vats if str(v).strip()]
                except Exception:
                    current_app.logger.warning("Failed to parse backup_manifest.json from backup", exc_info=True)

        summary["customers_total"] = customer_count
        summary["vat_total"] = len(vats)
        if vats:
            summary["sample_vats"] = sorted(vats)[:10]
        if timestamps:
            summary["modified_first"] = min(timestamps).isoformat()
            summary["modified_last"] = max(timestamps).isoformat()

    try:
        file_like.seek(0)
    except Exception:
        pass

    return summary


def _path_matches_selected_vat(rel_path: str, selected_vats: set[str]) -> bool:
    """Best-effort check whether a file path belongs to one of the selected VATs."""
    if not rel_path or not selected_vats:
        return False
    rel = str(rel_path).replace('\\', '/').lower()
    for vat in selected_vats:
        v = str(vat).strip().lower()
        if not v:
            continue
        if f"/{v}_" in rel or f"/{v}." in rel or f"/{v}/" in rel:
            return True
        if rel.startswith(f"{v}_") or rel.startswith(f"{v}."):
            return True
        if f"_{v}_" in rel or rel.endswith(f"_{v}.json"):
            return True
    return False


def _apply_backup_zip(zip_path: str) -> None:
    os.makedirs(get_group_base_dir(), exist_ok=True)
    base = os.path.normpath(get_group_base_dir())
    has_credentials = False

    with _open_backup_zip(zip_path) as zf:
        selected_vats: set[str] = set()
        mode = "group"
        try:
            if "backup_manifest.json" in zf.namelist():
                with zf.open("backup_manifest.json") as mf:
                    manifest = json.loads(mf.read().decode("utf-8-sig"))
                if isinstance(manifest, dict):
                    m = str(manifest.get("mode") or "").strip().lower()
                    if m in {"group", "customer"}:
                        mode = m
                    vats = manifest.get("selected_vats")
                    if isinstance(vats, list):
                        selected_vats = {str(v).strip() for v in vats if str(v).strip()}
        except Exception:
            current_app.logger.warning("Failed to parse backup manifest during restore", exc_info=True)

        members = [info for info in zf.infolist() if not info.is_dir()]
        if not members:
            raise ValueError("Το backup δεν περιέχει αρχεία.")

        customer_mode = mode == "customer" and bool(selected_vats)
        restored_credentials_payload = None

        for info in members:
            rel = _normalize_backup_member(info.filename)
            if not rel:
                continue
            if rel == "backup_manifest.json":
                continue

            if customer_mode and rel in {"credentials_settings.json", "fiscal_meta.json", "activity.log"}:
                # customer restore should not overwrite group-wide shared state files
                continue

            if customer_mode and rel not in {"credentials.json"} and not _path_matches_selected_vat(rel, selected_vats):
                continue

            if rel == "credentials.json":
                has_credentials = True
                if customer_mode:
                    try:
                        with zf.open(info) as src:
                            restored_credentials_payload = json.loads(src.read().decode("utf-8-sig"))
                    except Exception:
                        current_app.logger.warning("Invalid credentials.json in customer backup; continuing without credentials merge", exc_info=True)
                        restored_credentials_payload = None
                    continue

            dest = os.path.normpath(os.path.join(get_group_base_dir(), rel))
            if not dest.startswith(base + os.sep) and dest != base:
                raise ValueError(f"Μη έγκυρη διαδρομή στο backup: {info.filename}")
            dest_dir = os.path.dirname(dest)
            if dest_dir:
                os.makedirs(dest_dir, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)

        if customer_mode and has_credentials:
            cred_path = os.path.join(get_group_base_dir(), "credentials.json")
            try:
                current = _safe_json_read(cred_path, default=[])
                current_list = current if isinstance(current, list) else []
                incoming_list = restored_credentials_payload if isinstance(restored_credentials_payload, list) else []

                incoming_by_vat = {}
                for item in incoming_list:
                    if isinstance(item, dict):
                        v = str(item.get("vat") or "").strip()
                        if v:
                            incoming_by_vat[v] = item

                merged = []
                seen = set()
                for item in current_list:
                    if not isinstance(item, dict):
                        continue
                    v = str(item.get("vat") or "").strip()
                    if v and v in incoming_by_vat and v in selected_vats:
                        merged.append(incoming_by_vat[v])
                        seen.add(v)
                    else:
                        merged.append(item)

                for v in selected_vats:
                    if v in incoming_by_vat and v not in seen:
                        merged.append(incoming_by_vat[v])

                json_write(cred_path, merged)
            except Exception:
                raise ValueError("Αποτυχία συγχώνευσης credentials κατά την επαναφορά.")

    if not has_credentials and mode != "customer":
        raise ValueError("Το backup δεν περιέχει το αρχείο credentials.json.")


def _derive_backup_password() -> str:
    """Derive a deterministic AES backup password from MASTER_ENCRYPTION_KEY.
    Returns a 16-character hex string (64-bit); stable across calls."""
    import hashlib
    master = os.getenv("MASTER_ENCRYPTION_KEY", "")
    if master:
        digest = hashlib.sha256(master.encode("utf-8")).hexdigest()
        return digest[:16]
    # Fallback: fixed default (low security — warns in log)
    current_app.logger.warning(
        "MASTER_ENCRYPTION_KEY not set; backup encrypted with default password. "
        "Set MASTER_ENCRYPTION_KEY for production."
    )
    return "scanmydata_bkup"


@app.get("/api/data_backup/download")
def data_backup_download():
    # only group admin may download backups
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            return jsonify({'ok': False, 'error': 'no active group selected'}), 403
        if not getattr(current_user, 'is_authenticated', False) or current_user.role_for_group(grp) != 'admin':
            return jsonify({'ok': False, 'error': 'admin privileges required'}), 403
    except Exception:
        return jsonify({'ok': False, 'error': 'permission check failed'}), 500

    try:
        import pyzipper

        # Get backup mode and customer list from query params
        mode = request.args.get('mode', 'group').strip().lower()
        customers_str = request.args.get('customers', '').strip()
        selected_customers = set(v.strip() for v in customers_str.split(',') if v.strip()) if customers_str else set()

        backup_password = _derive_backup_password().encode('utf-8')

        mem = io.BytesIO()
        with pyzipper.AESZipFile(mem, "w", compression=pyzipper.ZIP_DEFLATED,
                                  encryption=pyzipper.WZ_AES) as zf:
            zf.setpassword(backup_password)
            base = get_group_base_dir()

            if mode == 'customer' and selected_customers:
                manifest = {
                    "mode": "customer",
                    "selected_vats": sorted(list(selected_customers)),
                    "created_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                }
                zf.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))

                creds_path = os.path.join(base, "credentials.json")
                if os.path.exists(creds_path):
                    try:
                        creds_data = _safe_json_read(creds_path, default=[])
                        creds_list = creds_data if isinstance(creds_data, list) else []
                        filtered = [
                            c for c in creds_list
                            if isinstance(c, dict) and str(c.get("vat") or "").strip() in selected_customers
                        ]
                        zf.writestr("credentials.json", json.dumps(filtered, ensure_ascii=False, indent=2).encode("utf-8"))
                    except Exception:
                        current_app.logger.warning("Failed to build filtered credentials.json for customer backup", exc_info=True)

                for root, _, files in os.walk(base):
                    for fname in files:
                        if fname in {'credentials.json', 'credentials_settings.json', 'activity.log', 'fiscal_meta.json', 'backup_manifest.json'}:
                            continue
                        path = os.path.join(root, fname)
                        arc = os.path.relpath(path, base)
                        if _path_matches_selected_vat(arc, selected_customers):
                            zf.write(path, arc)
            else:
                manifest = {
                    "mode": "group",
                    "selected_vats": [],
                    "created_at": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                }
                zf.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
                for root, _, files in os.walk(base):
                    for fname in files:
                        path = os.path.join(root, fname)
                        arc = os.path.relpath(path, base)
                        zf.write(path, arc)

        mem.seek(0)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_label = f"backup_{mode}" if mode == 'customer' else "backup"
        file_name = f"data_{backup_label}_{ts}.zip"

        file_size_mb = len(mem.getvalue()) / (1024 * 1024)

        try:
            from utils import log_user_activity
            log_user_activity(
                user_id=getattr(current_user, 'id', None) or getattr(current_user, 'pw_hash', None),
                group_name=grp.name if grp else 'unknown',
                action='backup_download',
                details={
                    'file_name': file_name,
                    'file_size_mb': round(file_size_mb, 2),
                    'mode': mode,
                    'encrypted': True,
                    'customers': list(selected_customers) if mode == 'customer' else None,
                    'customers_count': len(selected_customers) if mode == 'customer' else None,
                },
                user_email=getattr(current_user, 'email', None),
                user_username=getattr(current_user, 'username', None)
            )
        except Exception as e:
            current_app.logger.error(f"Failed to log backup download: {e}")

        return send_file(
            mem,
            mimetype="application/zip",
            as_attachment=True,
            download_name=file_name,
        )
    except Exception as exc:
        current_app.logger.exception("data_backup_download failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/data_backup/inspect")
def data_backup_inspect():
    # only group admin may inspect backup contents
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            return jsonify({'ok': False, 'error': 'no active group selected'}), 403
        if not getattr(current_user, 'is_authenticated', False) or current_user.role_for_group(grp) != 'admin':
            return jsonify({'ok': False, 'error': 'admin privileges required'}), 403
    except Exception:
        return jsonify({'ok': False, 'error': 'permission check failed'}), 500

    uploaded = request.files.get("backup_file")
    if not uploaded:
        return jsonify({"ok": False, "error": "Δεν επιλέχθηκε αρχείο."}), 400
    try:
        summary = _analyze_backup_zip(uploaded.stream)
        return jsonify({"ok": True, "summary": summary})
    except zipfile.BadZipFile:
        return jsonify({"ok": False, "error": "Μη έγκυρο αρχείο ZIP."}), 400
    except Exception as exc:
        current_app.logger.exception("data_backup_inspect failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    finally:
        try:
            uploaded.stream.seek(0)
        except Exception:
            pass


@app.post("/api/data_backup/restore")
def data_backup_restore():
    # only group admin may restore backups
    try:
        from auth import get_active_group
        from flask_login import current_user
        grp = get_active_group()
        if not grp:
            return jsonify({'ok': False, 'error': 'no active group selected'}), 403
        if not getattr(current_user, 'is_authenticated', False) or current_user.role_for_group(grp) != 'admin':
            return jsonify({'ok': False, 'error': 'admin privileges required'}), 403
    except Exception:
        return jsonify({'ok': False, 'error': 'permission check failed'}), 500

    uploaded = request.files.get("backup_file")
    if not uploaded:
        return jsonify({"ok": False, "error": "Δεν επιλέχθηκε αρχείο."}), 400

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            uploaded.stream.seek(0)
            shutil.copyfileobj(uploaded.stream, tmp)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as fh:
            summary = _analyze_backup_zip(fh)
        restore_mode = str(summary.get("mode") or "group").strip().lower()
        if not summary.get("contains_credentials") and restore_mode != "customer":
            raise ValueError("Το backup δεν περιέχει credentials.json.")

        _apply_backup_zip(tmp_path)
        
        # Log backup upload/restore with enhanced details
        try:
            from utils import log_user_activity
            file_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
            log_user_activity(
                user_id=getattr(current_user, 'id', None) or getattr(current_user, 'pw_hash', None),
                group_name=grp.name if grp else 'unknown',
                action='backup_upload',
                details={
                    'file_name': uploaded.filename,
                    'file_size_mb': round(file_size_mb, 2),
                    'files_count': summary.get('total_files', 0),
                    'contains_credentials': summary.get('contains_credentials', False),
                    'summary': summary
                },
                user_email=getattr(current_user, 'email', None),
                user_username=getattr(current_user, 'username', None)
            )
        except Exception as e:
            current_app.logger.error(f"Failed to log backup upload: {e}")
        
        return jsonify({"ok": True, "summary": summary})
    except zipfile.BadZipFile:
        return jsonify({"ok": False, "error": "Μη έγκυρο αρχείο ZIP."}), 400
    except ValueError as ve:
        return jsonify({"ok": False, "error": str(ve)}), 400
    except Exception:
        current_app.logger.exception("data_backup_restore failed")
        return jsonify({"ok": False, "error": "Αποτυχία επαναφοράς backup."}), 500
    finally:
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

# ---------------- List / download ----------------
@app.route("/list", methods=["GET"])
def list_invoices():
    # choose excel file based on active session credential
    active = get_active_credential_from_session()
    excel_path = DEFAULT_EXCEL_FILE
    if active and active.get("vat"):
        excel_path = excel_path_for(vat=active.get("vat"))
    elif active and active.get("name"):
        excel_path = excel_path_for(cred_name=active.get("name"))
    else:
        excel_path = DEFAULT_EXCEL_FILE

    # download
    if request.args.get("download") and os.path.exists(excel_path):
        # Log Excel download
        try:
            from utils import log_user_activity
            from flask_login import current_user
            from auth import get_active_group
            import pandas as pd
            
            file_size_mb = os.path.getsize(excel_path) / (1024 * 1024)
            grp = get_active_group()
            
            # Get book category from active credential (same as export bridge)
            book_category = ''
            if active and isinstance(active, dict):
                try:
                    book_category = str(active.get("book_category") or "").strip()
                except Exception:
                    book_category = ''
            
            # Read Excel to count rows
            rows_count = 0
            try:
                df = pd.read_excel(excel_path, engine="openpyxl")
                rows_count = len(df)
            except Exception as read_err:
                current_app.logger.warning(f"Could not read Excel for row count: {read_err}")
            
            log_user_activity(
                user_id=getattr(current_user, 'id', None) or getattr(current_user, 'pw_hash', None),
                group_name=grp.name if grp else 'unknown',
                action='export_expenses',
                details={
                    'file_name': os.path.basename(excel_path),
                    'file_size_mb': round(file_size_mb, 2),
                    'rows_count': rows_count,
                    'book_category': book_category,
                    'vat': active.get('vat') if active else None
                },
                user_email=getattr(current_user, 'email', None),
                user_username=getattr(current_user, 'username', None)
            )
        except Exception as e:
            current_app.logger.error(f"Failed to log Excel download: {e}")
        
        return send_file(
            excel_path,
            as_attachment=True,
            download_name=os.path.basename(excel_path),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    table_html = ""
    error = ""
    css_numcols = ""

    try:
        active_vat = str((active or {}).get("vat") or "").strip()
        table_html, file_exists, _ = _render_table_html_for_vat(active_vat, with_checkbox_value=True)
        if not file_exists:
            error = "Δεν βρέθηκαν εγγραφές στο epsilon_invoices.json."
    except Exception as e:
        file_exists = False
        error = f"Σφάλμα ανάγνωσης epsilon_invoices.json: {e}"

    active_name = session.get("active_credential")
    return safe_render(
        "list.html",
        table_html=Markup(table_html),
        error=error,
        file_exists=file_exists,
        css_numcols=css_numcols,
        active_page="list_invoices",
        active_credential=active_name
    )


@app.route('/list/fragment', methods=['GET'])
def list_fragment():
    """Return only the HTML table fragment for the current active VAT/group.
    Used by client-side partial refresh when other users update the same VAT.
    """
    try:
        active = get_active_credential_from_session() or {}
        active_vat = str(active.get("vat") or "").strip()
        highlight_mark = str(request.args.get("highlight_mark") or "").strip()
        table_html, exists, _ = _render_table_html_for_vat(active_vat, with_checkbox_value=True)
        if not exists:
            table_html = '<div class="p-3 text-gray-500">Δεν βρέθηκαν εγγραφές στο epsilon_invoices.json.</div>'

        return jsonify({
            "ok": True,
            "table_html": table_html,
            "file_exists": bool(exists),
            "highlight_mark": highlight_mark,
        })
    except Exception as exc:
        current_app.logger.exception('list_fragment failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/activity/check_updates', methods=['GET'])
def activity_check_updates():
    """Return recent activity events relevant to current active credential (vat) and group.
    Used by `list_inner` to detect changes made by other connected users for the same active client.
    """
    try:
        from flask_login import current_user
        from auth import get_active_group
        from utils import log_user_activity
        grp = get_active_group()
        active = get_active_credential_from_session()
        vat = active.get('vat') if isinstance(active, dict) else None

        # look back window (seconds)
        lookback = int(request.args.get('lookback', 30))
        cutoff_ts = None
        try:
            from datetime import datetime, timezone, timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=lookback)
            cutoff_ts = cutoff.isoformat()
        except Exception:
            cutoff_ts = None

        # get recent activity for this group
        events = []
        try:
            import firebase_config
            folder = grp.name if grp else (grp.data_folder if grp and getattr(grp, 'data_folder', None) else 'unknown')
            raw = firebase_config.firebase_get_group_activity_logs(folder, limit=50) or []
            for e in raw:
                try:
                    # skip entries by current user
                    if str(e.get('user_id') or '') == str(getattr(current_user, 'id', '') or getattr(current_user, 'pw_hash', '')):
                        continue
                    # time filter
                    ts = e.get('timestamp')
                    if cutoff_ts and ts and ts < cutoff_ts:
                        continue
                    details = e.get('details') or {}
                    # filter by vat/client if available
                    candidate_vat = None
                    if isinstance(details, dict):
                        candidate_vat = details.get('vat') or details.get('customers') or details.get('customers_count')
                        # customers may be list of vats
                        if isinstance(candidate_vat, list) and vat:
                            if str(vat) not in [str(x) for x in candidate_vat]:
                                continue
                        # if vat is a scalar and matches
                        if isinstance(candidate_vat, (str, int)) and vat and str(candidate_vat) != str(vat):
                            # not matching
                            continue
                    # If no vat in details but action is delete_rows and excel_path exists, include only if active vat matches excel filename heuristics
                    action = e.get('action')
                    if vat and action in ('delete_rows', 'save_invoice', 'add_invoice', 'insert_row'):
                        # if details.excel_path present, try to match vat inside filename
                        ex = details.get('excel_path') or ''
                        if ex and str(vat) not in str(ex):
                            continue

                    # passed filters — include event
                    actor = details.get('user_email') or details.get('user_username') or e.get('user_id') or 'unknown'
                    summary = details.get('description') or details.get('message') or action
                    events.append({
                        'action': action,
                        'actor': actor,
                        'timestamp': e.get('timestamp'),
                        'summary': summary,
                        'details': details
                    })
                except Exception:
                    continue
        except Exception:
            events = []

        return jsonify({'ok': True, 'events': events})
    except Exception as exc:
        current_app.logger.exception('activity_check_updates failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500

# --- νέο route: προεπισκόπηση Epsilon (ίδιο tab) ---
@app.route("/epsilon/preview")
def epsilon_preview():
    vat = (request.args.get("vat") or (get_active_credential_from_session() or {}).get("vat") or "").strip()
    cred_for_labels = get_cred_by_vat(vat) or get_active_credential_from_session()
    category_labels = dict(DEFAULT_INVOICE_CATEGORY_LABELS)
    if cred_for_labels:
        try:
            category_labels.update(_category_labels_for_client(cred_for_labels))
        except Exception:
            current_app.logger.exception("Failed to build category labels for epsilon preview")
    
    # Έλεγχος για book_category
    book_category = ""
    try:
        creds = _safe_json_read(credentials_path_for_request(), default=[])
        cl = creds if isinstance(creds, list) else [creds]
        active = next((c for c in cl if str(c.get("vat")) == str(vat)), (cl[0] if cl else {}))
        book_category = str((active or {}).get("book_category") or "").strip().upper()
    except Exception:
        pass
    
    is_g_category = book_category == "Γ" or book_category == "G"
    
    # Επιλογή του κατάλληλου module
    if is_g_category:
        from epsilon_bridge_g_category import build_preview_rows_for_ui_g as build_preview_func
    else:
        from epsilon_bridge_multiclient_strict import build_preview_rows_for_ui as build_preview_func
    
    client_db_path = _resolve_client_db_path(vat)
    
    # Διάβασμα ενεργού έτους για φιλτράρισμα
    fiscal_year = None
    try:
        from epsilon_bridge_multiclient_strict import _read_active_fiscal_year
        fiscal_year = _read_active_fiscal_year(group_path("epsilon"))
    except Exception:
        pass
    
    rows, issues, _ok = build_preview_func(
        vat=vat,
        credentials_json=credentials_path_for_request(),
        cred_settings_json=settings_file_path(),
        invoices_json=None,
        client_db=client_db_path,
        base_invoices_dir=group_path("epsilon"),
        fiscal_year=fiscal_year,
    )

    # NOTE:
    # Η προεπισκόπηση epsilon βασίζεται στο epsilon_invoices.json.
    # Δεν εμφανίζουμε πλέον warning ασυμφωνίας με invoices.xlsx στη σελίδα preview.
    missing_excel_marks: List[str] = []
    missing_excel_rows: List[Dict[str, Any]] = []

    # πέρασέ τα στο template
    return render_template("epsilon_preview.html",
                           vat=vat,
                           table_rows=rows,
                           bridge_ok=(not issues and len(rows)>0),
                           bridge_issues=issues,
                           category_labels=category_labels,
                           missing_excel_marks=missing_excel_marks,
                           missing_excel_rows=missing_excel_rows)


@app.route("/export/fastimport/kinitseis")
def export_fastimport_kinitseis():
    vat = request.args.get("vat") or ""
    confirm = request.args.get("confirm_new_partners") == "1"

    # Προαιρετικό φιλτράρισμα από το preview: MARKs που ο χρήστης διέγραψε πριν το export
    excluded_marks_raw = (request.args.get("excluded_marks") or "").strip()
    excluded_marks = {m.strip() for m in excluded_marks_raw.split(",") if m.strip()}
    temp_invoices_json: Optional[str] = None

    if excluded_marks:
        try:
            from epsilon_bridge_multiclient_strict import resolve_paths_for_vat, load_epsilon_invoices
            paths_for_invoices = resolve_paths_for_vat(vat, None, None, None, group_path("epsilon"))
            all_invoices = load_epsilon_invoices(paths_for_invoices["invoices"])
            filtered_invoices = []
            for rec in (all_invoices or []):
                rec_mark = str(rec.get("MARK") or rec.get("mark") or "").strip()
                if rec_mark and rec_mark in excluded_marks:
                    continue
                filtered_invoices.append(rec)

            with tempfile.NamedTemporaryFile(mode="w", suffix="_filtered_epsilon_invoices.json", delete=False, encoding="utf-8") as tf:
                json.dump(filtered_invoices, tf, ensure_ascii=False, indent=2)
                temp_invoices_json = tf.name
        except Exception:
            current_app.logger.exception("Failed to apply excluded MARKs filter for epsilon export")
            temp_invoices_json = None

    @after_this_request
    def _cleanup_temp_filtered_file(response):
        if temp_invoices_json:
            try:
                os.remove(temp_invoices_json)
            except Exception:
                pass
        return response

    # Διάβασε active credential για να δούμε αν είμαστε σε AFM mode και book_category
    apod_type = ""
    active_cred = None
    book_category = ""
    try:
        creds = _safe_json_read(credentials_path_for_request(), default=[])
        cl = creds if isinstance(creds, list) else [creds]
        active = next((c for c in cl if str(c.get("vat")) == str(vat)), (cl[0] if cl else {}))
        active_cred = active if isinstance(active, dict) else {}
        apod_type = str((active or {}).get("apodeixakia_type", "")).lower()
        book_category = str((active or {}).get("book_category") or "").strip().upper()
    except Exception:
        pass

    is_b_category = book_category == "Β" or book_category == "B"
    is_g_category = book_category == "Γ" or book_category == "G"

    base_client_db = _resolve_client_db_path(vat)
    invoices_fallback = os.path.join("data", "epsilon", vat, f"{vat}_epsilon_invoices.json")

    # Επιλογή του κατάλληλου module ανάλογα με την κατηγορία
    if is_g_category:
        # Χρήση Γ Κατηγορίας module
        from epsilon_bridge_g_category import build_preview_strict_g_category as build_preview
        from epsilon_bridge_g_category import export_g_category as export_func
    else:
        # Χρήση Β Κατηγορίας (default)
        from epsilon_bridge_multiclient_strict import (
            build_preview_strict_multiclient as build_preview,
            export_multiclient_strict as export_func,
        )

    # 1) Preview για να εντοπίσουμε receipts χωρίς CUSTID
    # Διάβασμα ενεργού έτους
    fiscal_year = None
    try:
        from epsilon_bridge_multiclient_strict import _read_active_fiscal_year
        fiscal_year = _read_active_fiscal_year(group_path("epsilon"))
    except Exception:
        pass
    
    preview = build_preview(
        vat=vat,
        credentials_json=credentials_path_for_request(),
        cred_settings_json=settings_file_path(),
        invoices_json=temp_invoices_json,
        client_db=base_client_db,
        base_invoices_dir=group_path("epsilon"),
        fiscal_year=fiscal_year,
    )

    if apod_type == "afm" and not confirm:
        missing_map = _collect_missing_afm_from_preview(preview.get("rows"))
        if missing_map:
            # Δείξε modal επιβεβαίωσης
            return render_template(
                "export_confirm_partners.html",
                vat=vat,
                missing=list(missing_map.values()),
                confirm_url=url_for("export_fastimport_kinitseis", vat=vat, confirm_new_partners="1"),
                cancel_url=url_for("search", vat=vat),
            )

    # 2) Αν επιβεβαιώθηκε (AFM mode) -> φτιάξε προσωρινό client_db με νέους CUSTID
    client_db_path = base_client_db
    if apod_type == "afm" and confirm:
        missing_map = _collect_missing_afm_from_preview(preview.get("rows"))
        if missing_map:
            client_db_path = _make_temp_client_db_with_new_ids(
                vat=vat,
                base_client_db_path=base_client_db,
                missing_map=missing_map,
                invoices_fallback_json=invoices_fallback,
            )

    # 3) Κανονικό export (χρησιμοποιεί το σωστό module ανάλογα με book_category)
    # Διάβασμα ενεργού έτους
    fiscal_year = None
    try:
        from epsilon_bridge_multiclient_strict import _read_active_fiscal_year
        fiscal_year = _read_active_fiscal_year(group_path("epsilon"))
    except Exception:
        pass
    
    ok, out_path, issues = export_func(
        vat=vat,
        credentials_json=credentials_path_for_request(),
        cred_settings_json=settings_file_path(),
        invoices_json=temp_invoices_json,
        client_db=client_db_path,
        out_xlsx=None,
        base_invoices_dir=group_path("epsilon"),
        base_exports_dir=group_path("exports"),
        fiscal_year=fiscal_year,
    )

    if ok and out_path:
        # Calculate file size and row count for logging
        file_size_mb = 0
        rows_count = 0
        try:
            if os.path.exists(out_path):
                file_size_mb = os.path.getsize(out_path) / (1024 * 1024)
                import pandas as pd
                df_temp = pd.read_excel(out_path, engine="openpyxl")
                rows_count = len(df_temp)
        except Exception:
            pass
        
        # Log export with enhanced details
        try:
            from utils import log_user_activity
            from flask_login import current_user
            from auth import get_active_group
            grp = get_active_group()
            log_user_activity(
                user_id=getattr(current_user, 'id', None) or getattr(current_user, 'pw_hash', None),
                group_name=grp.name if grp else 'unknown',
                action='export_bridge',
                details={
                    'book_category': book_category if book_category else ('Β' if is_b_category else 'Γ'),
                    'rows_count': rows_count,
                    'file_size_mb': round(file_size_mb, 2),
                    'file_name': os.path.basename(out_path),
                    'includes_b_kat': is_b_category and os.path.exists(os.path.join(BASE_DIR, "b_kat.ect")),
                    'includes_g_kat': is_g_category and os.path.exists(os.path.join(BASE_DIR, "Γ.ect")),
                    'vat': vat
                },
                user_email=getattr(current_user, 'email', None),
                user_username=getattr(current_user, 'username', None)
            )
        except Exception as e:
            current_app.logger.error(f"Failed to log export activity: {e}")
        
        # Bundling με .ect file ανάλογα με την κατηγορία
        ect_file = None
        if is_b_category:
            bkat_path = os.path.join(BASE_DIR, "b_kat.ect")
            if os.path.exists(bkat_path):
                ect_file = ("b_kat.ect", bkat_path)
        elif is_g_category:
            # Προτιμάμε το g_kat.ect αν υπάρχει, αλλιώς το Γ.ect
            gkat_path = os.path.join(BASE_DIR, "g_kat.ect")
            if os.path.exists(gkat_path):
                ect_file = ("g_kat.ect", gkat_path)
            else:
                gkat_path_alt = os.path.join(BASE_DIR, "Γ.ect")
                if os.path.exists(gkat_path_alt):
                    ect_file = ("Γ.ect", gkat_path_alt)
        
        if ect_file:
            ect_name, ect_path = ect_file
            fd, temp_path = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            zip_name_root, _ = os.path.splitext(os.path.basename(out_path))
            if not zip_name_root:
                zip_name_root = "epsilon_bridge"
            zip_filename = f"{zip_name_root}_with_{ect_name.replace('.ect', '')}.zip"
            try:
                with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    zf.write(out_path, arcname=os.path.basename(out_path))
                    zf.write(ect_path, arcname=ect_name)

                @after_this_request
                def _cleanup_temp_archive(response):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                    try:
                        if out_path and os.path.exists(out_path):
                            os.remove(out_path)
                    except OSError:
                        pass
                    return response

                return send_file(temp_path, as_attachment=True, download_name=zip_filename)
            except Exception:
                cat_label = "Β" if is_b_category else "Γ"
                current_app.logger.exception(f"Failed to bundle {ect_name} with {cat_label} category export")
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

        @after_this_request
        def _cleanup_export_after_download(response):
            try:
                if out_path and os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return response

        return send_file(out_path, as_attachment=True)

    # fallback: δείξε ό,τι άλλο πρόβλημα επιστρέφει
    for it in (issues or []):
        flash(it.get("message") or "Αποτυχία εξαγωγής.", "error")
    return redirect(url_for("search", vat=vat))


# --- μικρά helpers για parsing από μηνύματα (προαιρετικά) ---
def _extract_afm_from_msg(msg: str) -> str:
    m = re.search(r"(?:AFM|ΑΦΜ)\s+(\d{9})", msg or "", flags=re.I)
    return m.group(1) if m else ""

def _extract_aa_from_msg(msg: str) -> str:
    m = re.search(r"(?:AA=|ΑΑ=)\s*(\d+)", msg or "", flags=re.I)
    return m.group(1) if m else ""

def _looks_like_receipt(rec: dict) -> bool:
    t = f"{rec.get('DOCTYPE','')} {rec.get('type','')} {rec.get('category','')}".lower()
    return any(k in t for k in ("receipt", "αποδειξ", "λιαν"))


DELETE_UNDO_MAX = 5
DELETE_UNDO_TTL_SECONDS = 60 * 60
DELETE_UNDO_STACKS: Dict[str, List[Dict[str, Any]]] = {}


def _delete_undo_scope_key() -> str:
    try:
        active = get_active_credential_from_session() or {}
    except Exception:
        active = {}

    vat = str(active.get("vat") or "default").strip() or "default"
    name = str(active.get("name") or "default").strip() or "default"

    try:
        from auth import get_active_group
        grp = get_active_group()
        group_name = str(getattr(grp, "name", "") or "default").strip() or "default"
    except Exception:
        group_name = "default"

    try:
        uid = str(getattr(current_user, "id", "") or getattr(current_user, "pw_hash", "") or "anon").strip() or "anon"
    except Exception:
        uid = "anon"
    return f"{uid}:{group_name}:{vat}:{name}"


def _cleanup_delete_undo_stack(scope_key: str):
    now = int(time.time())
    stack = DELETE_UNDO_STACKS.get(scope_key) or []
    stack = [e for e in stack if int(e.get("created_ts") or 0) >= (now - DELETE_UNDO_TTL_SECONDS)]
    if stack:
        DELETE_UNDO_STACKS[scope_key] = stack[:DELETE_UNDO_MAX]
    else:
        DELETE_UNDO_STACKS.pop(scope_key, None)


def _push_delete_undo_entry(entry: Dict[str, Any]):
    scope_key = _delete_undo_scope_key()
    _cleanup_delete_undo_stack(scope_key)
    stack = DELETE_UNDO_STACKS.get(scope_key) or []
    stack.insert(0, entry)
    DELETE_UNDO_STACKS[scope_key] = stack[:DELETE_UNDO_MAX]


def _extract_mark_from_any(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for k in ("mark", "MARK", "invoice_id", "Αριθμός Μητρώου", "id"):
        if k in item and item.get(k) not in (None, ""):
            return str(item.get(k)).strip()
    return ""


def _find_delete_undo_entry(scope_key: str, token: str):
    _cleanup_delete_undo_stack(scope_key)
    stack = DELETE_UNDO_STACKS.get(scope_key) or []
    for idx, e in enumerate(stack):
        if str(e.get("token") or "") == str(token or ""):
            return stack, idx, e
    return stack, -1, None


@app.route("/delete/undo", methods=["POST"])
def delete_undo():
    is_ajax_request = (
        (request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest")
        or (request.args.get("ajax") == "1")
        or (request.form.get("ajax") == "1")
    )
    token = str(request.form.get("token") or request.args.get("token") or "").strip()
    if not token:
        payload = request.get_json(silent=True) or {}
        token = str(payload.get("token") or "").strip()

    if not token:
        msg = "Δεν βρέθηκε ενέργεια για αναίρεση."
        if is_ajax_request:
            return jsonify({"ok": False, "error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("search"))

    scope_key = _delete_undo_scope_key()
    stack, idx, entry = _find_delete_undo_entry(scope_key, token)
    if idx < 0 or not entry:
        msg = "Η ενέργεια αναίρεσης έληξε ή δεν υπάρχει."
        if is_ajax_request:
            return jsonify({"ok": False, "error": msg}), 404
        flash(msg, "error")
        return redirect(url_for("search"))

    restored_excel = 0
    restored_epsilon = 0

    try:
        excel_rows = entry.get("excel_rows") if isinstance(entry.get("excel_rows"), list) else []
        excel_columns = entry.get("excel_columns") if isinstance(entry.get("excel_columns"), list) else []
        excel_path = str(entry.get("excel_path") or "").strip()
        if excel_rows and excel_path:
            import pandas as pd
            restore_df = pd.DataFrame(excel_rows)
            if excel_columns:
                for c in excel_columns:
                    if c not in restore_df.columns:
                        restore_df[c] = ""
                restore_df = restore_df[excel_columns]
            if os.path.exists(excel_path):
                cur_df = pd.read_excel(excel_path, engine="openpyxl", dtype=str).fillna("")
                cur_df.columns = [str(c).strip() for c in cur_df.columns.astype(str)]
                if "MARK" in cur_df.columns and "MARK" in restore_df.columns:
                    existing_marks = set(cur_df["MARK"].astype(str).str.strip().tolist())
                    restore_df = restore_df[~restore_df["MARK"].astype(str).str.strip().isin(existing_marks)]
                if not restore_df.empty:
                    merged = pd.concat([cur_df, restore_df], ignore_index=True)
                    merged.to_excel(excel_path, index=False, engine="openpyxl")
                    restored_excel = int(restore_df.shape[0])
            else:
                out_df = restore_df.copy()
                out_df.to_excel(excel_path, index=False, engine="openpyxl")
                restored_excel = int(out_df.shape[0])
    except Exception:
        log.exception("delete_undo: failed restoring Excel for token=%s", token)

    try:
        snapshots = entry.get("epsilon_snapshots") if isinstance(entry.get("epsilon_snapshots"), list) else []
        for snap in snapshots:
            if not isinstance(snap, dict):
                continue
            vat_code = str(snap.get("vat") or "").strip()
            rows = snap.get("rows") if isinstance(snap.get("rows"), list) else []
            if not rows:
                continue
            try:
                eps_path = epsilon_file_path_for(vat_code) if vat_code else ""
            except Exception:
                eps_path = ""
            if not eps_path:
                continue
            try:
                cache = json_read(eps_path) or []
            except Exception:
                cache = []
            existing_marks = set(_extract_mark_from_any(it) for it in cache if isinstance(it, dict))
            to_add = [r for r in rows if _extract_mark_from_any(r) and _extract_mark_from_any(r) not in existing_marks]
            if not to_add:
                continue
            new_cache = cache + to_add
            try:
                if globals().get("_safe_save_epsilon_cache"):
                    _safe_save_epsilon_cache(vat_code, new_cache)
                else:
                    json_write(eps_path, new_cache)
                restored_epsilon += len(to_add)
            except Exception:
                log.exception("delete_undo: failed restoring epsilon cache for vat=%s", vat_code)
    except Exception:
        log.exception("delete_undo: failed restoring epsilon snapshots token=%s", token)

    try:
        stack.pop(idx)
        if stack:
            DELETE_UNDO_STACKS[scope_key] = stack[:DELETE_UNDO_MAX]
        else:
            DELETE_UNDO_STACKS.pop(scope_key, None)
    except Exception:
        pass

    customer_name = str(entry.get("customer_name") or "").strip()
    customer_vat = str(entry.get("customer_vat") or "").strip()
    if customer_name and customer_vat:
        customer_part = f" για τον πελάτη {customer_name} ({customer_vat})"
    elif customer_name:
        customer_part = f" για τον πελάτη {customer_name}"
    elif customer_vat:
        customer_part = f" για τον πελάτη με ΑΦΜ {customer_vat}"
    else:
        customer_part = ""

    msg = f"Έγινε αναίρεση διαγραφής{customer_part}. Επαναφέρθηκαν από Excel: {restored_excel}, από Epsilon cache: {restored_epsilon}."
    if is_ajax_request:
        return jsonify({"ok": True, "message": msg, "restored_excel": restored_excel, "restored_epsilon": restored_epsilon}), 200
    flash(msg, "success")
    return redirect(url_for("search"))

# ---------------- Delete invoices ----------------
@app.route("/delete", methods=["POST"])
def delete_invoices():
    """
    Delete selected MARKs:
      - removes rows from active customer's Excel file (if MARK column exists)
      - removes matching entries from per-VAT epsilon cache (epsilon/..._epsilon_invoices.json)
    DOES NOT modify the per-customer invoices.json file.
    Extensive logging for debugging.
    """
    try:
        log.info("delete_invoices: request from %s form_keys=%s", request.remote_addr, list(request.form.keys()))
    except Exception:
        pass

    is_ajax_request = (
        (request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest")
        or (request.args.get("ajax") == "1")
        or (request.form.get("ajax") == "1")
    )

    # collect and normalize marks (primary)
    marks_to_delete = request.form.getlist("delete_mark") or []
    # fallback: maybe frontend sent JSON or comma-separated
    if not marks_to_delete:
        raw = request.form.get("delete_mark_json") or request.form.get("delete_marks") or request.form.get("marks")
        if raw:
            try:
                import json as _json
                parsed = _json.loads(raw)
                if isinstance(parsed, list):
                    marks_to_delete = parsed
            except Exception:
                marks_to_delete = [m.strip() for m in str(raw).split(",") if m.strip()]

    # normalize to strings and dedupe
    marks_to_delete = [str(m).strip() for m in marks_to_delete if str(m).strip()]
    marks_to_delete = list(dict.fromkeys(marks_to_delete))  # preserve order, dedupe

    log.info("delete_invoices: marks_to_delete resolved = %s", marks_to_delete)

    if not marks_to_delete:
        msg = "Δεν επιλέχθηκε κανένα MARK για διαγραφή."
        if is_ajax_request:
            return jsonify({"ok": False, "error": msg}), 400
        flash(msg, "error")
        return redirect(url_for("search"))

    # determine active customer's excel file
    active = get_active_credential_from_session()
    excel_path = DEFAULT_EXCEL_FILE
    if active and active.get("vat"):
        excel_path = excel_path_for(vat=active.get("vat"))
    elif active and active.get("name"):
        excel_path = excel_path_for(cred_name=active.get("name"))

    deleted_from_excel = 0
    deleted_excel_rows: List[Dict[str, Any]] = []
    deleted_excel_columns: List[str] = []
    try:
        if os.path.exists(excel_path):
            import pandas as pd
            df = pd.read_excel(excel_path, engine="openpyxl", dtype=str).fillna("")
            # normalize column names
            cols = [c.strip() for c in df.columns.astype(str)]
            df.columns = cols

            if "MARK" in df.columns:
                marks_series = df["MARK"].astype(str).str.strip()
                mask = marks_series.isin(marks_to_delete)
                num_matches = int(mask.sum())
                if num_matches > 0:
                    deleted_excel_rows = df[mask].copy().to_dict(orient="records")
                    deleted_excel_columns = list(df.columns)
                    df_remaining = df[~mask].copy()
                    try:
                        # If no rows remain, write an empty dataframe (preserving columns)
                        if df_remaining.shape[0] == 0:
                            empty_df = df.iloc[0:0].copy()
                            empty_df.to_excel(excel_path, index=False, engine="openpyxl")
                        else:
                            df_remaining.to_excel(excel_path, index=False, engine="openpyxl")
                        deleted_from_excel = num_matches
                        log.info("delete_invoices: deleted %d marks from Excel %s: %s", num_matches, excel_path, marks_to_delete)
                    except Exception:
                        log.exception("delete_invoices: failed writing Excel after deletion %s", excel_path)
                else:
                    log.info("delete_invoices: no matching MARKs found in Excel %s for deletion: %s", excel_path, marks_to_delete)
            else:
                log.warning("delete_invoices: Excel file %s does not contain a 'MARK' column; skipping Excel deletion", excel_path)
        else:
            log.info("delete_invoices: Excel path %s does not exist; skipping Excel deletion.", excel_path)
    except Exception:
        log.exception("delete_invoices: Error while deleting from Excel")

    # delete matching entries from per-VAT epsilon cache ONLY
    deleted_from_epsilon = 0
    deleted_epsilon_snapshots: List[Dict[str, Any]] = []
    try:
        vat = active.get("vat") if active else None
        if vat:
            # try helper to get epsilon path, else guess
            try:
                epsilon_path = epsilon_file_path_for(vat)
            except Exception:
                epsilon_path = os.path.join(group_path("epsilon"), f"{vat}_epsilon_invoices.json")

            if os.path.exists(epsilon_path):
                try:
                    eps_cache = json_read(epsilon_path) or []
                except Exception:
                    try:
                        with open(epsilon_path, "r", encoding="utf-8") as f:
                            eps_cache = json.load(f) or []
                    except Exception:
                        eps_cache = []

                before_len = len(eps_cache)

                removed_here = [e for e in eps_cache if _extract_mark_from_any(e) in marks_to_delete]
                new_cache = [e for e in eps_cache if _extract_mark_from_any(e) not in marks_to_delete]
                after_len = len(new_cache)
                deleted_from_epsilon = before_len - after_len
                if removed_here:
                    deleted_epsilon_snapshots.append({"vat": str(vat).strip(), "rows": removed_here})

                if deleted_from_epsilon > 0:
                    try:
                        # Prefer safe helper if available
                        if globals().get("_safe_save_epsilon_cache"):
                            _safe_save_epsilon_cache(vat, new_cache)
                        else:
                            try:
                                json_write(epsilon_path, new_cache)
                            except Exception:
                                tmp = epsilon_path + ".tmp"
                                with open(tmp, "w", encoding="utf-8") as f:
                                    json.dump(new_cache, f, ensure_ascii=False, indent=2)
                                os.replace(tmp, epsilon_path)
                        log.info("delete_invoices: Deleted %d marks from epsilon cache %s for VAT %s", deleted_from_epsilon, epsilon_path, vat)
                    except Exception:
                        log.exception("delete_invoices: failed to persist epsilon cache after deletion")
                else:
                    log.info("delete_invoices: No matching marks found in epsilon cache %s for deletion.", epsilon_path)
            else:
                log.info("delete_invoices: Epsilon cache does not exist at %s; skipping epsilon deletion.", epsilon_path)
        else:
            log.info("delete_invoices: No active VAT available; skipped epsilon deletion.")
    except Exception:
        log.exception("delete_invoices: Error while deleting from epsilon cache")
        # --- Fallback: if nothing was removed (or no active VAT), also scan all epsilon caches and remove these MARKs ---
    try:
        if deleted_from_epsilon == 0:
            eps_dir = group_path("epsilon")
            if os.path.isdir(eps_dir):
                for fname in os.listdir(eps_dir):
                    if not fname.endswith("_epsilon_invoices.json"):
                        continue
                    eps_path = os.path.join(eps_dir, fname)
                    # read cache
                    try:
                        eps_cache = json_read(eps_path) or []
                    except Exception:
                        try:
                            with open(eps_path, "r", encoding="utf-8") as f:
                                eps_cache = json.load(f) or []
                        except Exception:
                            eps_cache = []
                    before_len = len(eps_cache)

                    removed_here = [e for e in eps_cache if _extract_mark_from_any(e) in marks_to_delete]
                    new_cache = [e for e in eps_cache if _extract_mark_from_any(e) not in marks_to_delete]
                    if len(new_cache) != before_len:
                        # write back
                        try:
                            if globals().get("_safe_save_epsilon_cache"):
                                vat_code = fname.split("_epsilon_invoices.json")[0]
                                _safe_save_epsilon_cache(vat_code, new_cache)
                            else:
                                try:
                                    json_write(eps_path, new_cache)
                                except Exception:
                                    tmp = eps_path + ".tmp"
                                    with open(tmp, "w", encoding="utf-8") as f:
                                        json.dump(new_cache, f, ensure_ascii=False, indent=2)
                                    os.replace(tmp, eps_path)
                        except Exception:
                            log.exception("delete_invoices: fallback write failed for %s", eps_path)
                        # update counter
                        deleted_from_epsilon += (before_len - len(new_cache))
                        if removed_here:
                            vat_code = fname.split("_epsilon_invoices.json")[0]
                            deleted_epsilon_snapshots.append({"vat": str(vat_code).strip(), "rows": removed_here})
    except Exception:
        log.exception("delete_invoices: fallback cross-VAT epsilon deletion failed")

    # Final summary
    total_requested = len(marks_to_delete)
    summary_msg = f"Διαγράφηκαν {total_requested} επιλεγμένα mark(s). Αφαιρέθηκαν από Excel: {deleted_from_excel}, από Epsilon cache: {deleted_from_epsilon}"

    undo_token = ""
    undo_entry = None
    try:
        if deleted_from_excel > 0 or deleted_from_epsilon > 0:
            undo_token = secrets.token_urlsafe(10)
            undo_entry = {
                "token": undo_token,
                "created_ts": int(time.time()),
                "marks": list(marks_to_delete),
                "excel_path": excel_path,
                "excel_rows": deleted_excel_rows,
                "excel_columns": deleted_excel_columns,
                "epsilon_snapshots": deleted_epsilon_snapshots,
                "customer_vat": active.get("vat") if active else None,
                "customer_name": active.get("name") if active else None,
            }
            _push_delete_undo_entry(undo_entry)
    except Exception:
        log.exception("delete_invoices: failed to create undo entry")
    flash(summary_msg, "success")
    log.info("delete_invoices: finished request. requested=%d excel=%d epsilon=%d", total_requested, deleted_from_excel, deleted_from_epsilon)

    # Log deletion with enhanced details
    try:
        from utils import log_user_activity
        from flask_login import current_user
        from auth import get_active_group
        grp = get_active_group()
        log_user_activity(
            user_id=getattr(current_user, 'id', None) or getattr(current_user, 'pw_hash', None),
            group_name=grp.name if grp else 'unknown',
            action='delete_rows',
            details={
                'marks': marks_to_delete,
                'count': total_requested,
                'from_excel': deleted_from_excel,
                'from_epsilon': deleted_from_epsilon,
                'excel_path': os.path.basename(excel_path) if excel_path else None,
                'vat': active.get('vat') if active else None
            },
            user_email=getattr(current_user, 'email', None),
            user_username=getattr(current_user, 'username', None)
        )
    except Exception as e:
        log.error(f"Failed to log delete activity: {e}")

    if is_ajax_request:
        return jsonify({
            "ok": True,
            "message": summary_msg,
            "requested": total_requested,
            "deleted_from_excel": deleted_from_excel,
            "deleted_from_epsilon": deleted_from_epsilon,
            "marks": marks_to_delete,
            "undo": {
                "token": undo_token,
                "count": total_requested,
                "created_ts": (undo_entry or {}).get("created_ts"),
                "customer_vat": (undo_entry or {}).get("customer_vat"),
                "customer_name": (undo_entry or {}).get("customer_name"),
            } if undo_token else None,
        }), 200

    return redirect(url_for("search"))





# ---------------- Global error handler ----------------
@app.errorhandler(Exception)
def handle_unexpected_error(e):
    tb = traceback.format_exc()
    log.error("Unhandled exception: %s\n%s", str(e), tb)
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    if debug:
        return "<pre>{}</pre>".format(escape(tb)), 500
    return safe_render("error_generic.html", message="Συνέβη σφάλμα στον server. Δες logs."), 500

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route("/credentials", methods=["GET", "POST"])
def credentials_page():
    msg = None
    if request.method == "POST":
        name = request.form.get("name","").strip()
        user = request.form.get("user","").strip()
        key = request.form.get("key","").strip()
        env = request.form.get("env","sandbox").strip()
        vat = request.form.get("vat","").strip()
        if not name:
            msg = ("error","Name required")
        else:
            ok, err = add_credential({"name":name,"user":user,"key":key,"env":env,"vat":vat})
            msg = ("success","Saved") if ok else ("error", err or "Could not save")
    creds = load_credentials()
    # simple HTML listing (if you have template file use render_template instead)
    html = "<h1>Credentials</h1><p><a href='/'>Back</a></p><ul>"
    for c in creds:
        html += f"<li><strong>{c.get('name')}</strong> - VAT: {c.get('vat','')}</li>"
    html += "</ul>"
    return html

@app.route("/health")
def health():
    return "OK"


# ============================================================================
# ADMIN PANEL ROUTES (Admin-only access)
# ============================================================================

def _require_admin(f):
    """Decorator to require admin access"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not (current_user.is_authenticated and admin_panel.is_admin(current_user)):
            flash('Admin access required', 'danger')
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function


@app.route("/admin")
@login_required
@_require_admin
def admin_dashboard():
    """Admin dashboard - overview of system (unified)"""
    try:
        # Load recent activity logs to show on dashboard
        try:
            from admin_panel import admin_get_activity_logs
            recent_activity = admin_get_activity_logs(limit=10) or []
        except Exception:
            recent_activity = []

        return render_template('admin/dashboard_unified.html', recent_activity=recent_activity)
    except Exception as e:
        logger.exception(f"Admin dashboard error: {e}")
        flash(f'Error: {str(e)}', 'danger')
        return redirect(url_for('home'))


@app.route("/admin/users")
@login_required
@_require_admin
def admin_users():
    """List and manage all users"""
    users = admin_panel.admin_list_all_users()
    return render_template('admin/users.html', users=users)


@app.route("/admin/users/<int:user_id>")
@login_required
@_require_admin
def admin_user_detail(user_id):
    """View user details"""
    user_detail = admin_panel.admin_get_user_details(user_id)
    if not user_detail:
        flash('User not found', 'danger')
        return redirect(url_for('admin_users'))
    # Return JSON for AJAX requests (modals)
    accept_json = request.is_json or request.headers.get('Accept') == 'application/json' or 'application/json' in request.headers.get('Accept', '')
    if accept_json:
        return jsonify(user_detail)

    return render_template('admin/user_detail.html', user=user_detail)


@app.route("/admin/users/<int:user_id>/delete", methods=['POST'])
@login_required
@_require_admin
def admin_user_delete(user_id):
    """Delete a user (supports both form and AJAX JSON)"""
    result = admin_panel.admin_delete_user(user_id, current_user)
    # If AJAX request, return JSON; else redirect
    if request.is_json or request.headers.get('Accept') == 'application/json':
        return jsonify(result)
    if result['ok']:
        flash(result['message'], 'success')
    else:
        flash(result['error'], 'danger')
    
    return redirect(url_for('admin_users'))


@app.route("/admin/groups")
@login_required
@_require_admin
def admin_groups():
    """List and manage all groups"""
    groups = admin_panel.admin_list_all_groups()
    return render_template('admin/groups.html', groups=groups)


@app.route("/admin/groups/<int:group_id>")
@login_required
@_require_admin
def admin_group_detail(group_id):
    """View group details"""
    group_detail = admin_panel.admin_get_group_details(group_id)
    if not group_detail:
        flash('Group not found', 'danger')
        return redirect(url_for('admin_groups'))
    # If this is an AJAX/JSON request, return JSON data for client-side modals
    accept_json = request.is_json or request.headers.get('Accept') == 'application/json' or 'application/json' in request.headers.get('Accept', '')
    if accept_json:
        return jsonify(group_detail)

    return render_template('admin/group_detail.html', group=group_detail)


@app.route("/admin/groups/<int:group_id>/backup", methods=['POST'])
@login_required
@_require_admin
def admin_group_backup(group_id):
    """Create backup of group"""
    backup_path = admin_panel.admin_backup_group(group_id)
    if backup_path:
        flash(f'Backup created: {backup_path}', 'success')
    else:
        flash('Failed to create backup', 'danger')
    
    return redirect(url_for('admin_group_detail', group_id=group_id))


@app.route("/admin/groups/<int:group_id>/files", methods=['GET'])
@login_required
@_require_admin
def admin_group_files(group_id):
    """View and manage files in a group"""
    from models import Group
    group = Group.query.get(group_id)
    if not group:
        flash('Group not found', 'danger')
        return redirect(url_for('admin_groups'))
    
    return render_template('admin/group_files.html', group=group)


@app.route("/admin/groups/<int:group_id>/delete", methods=['POST'])
@login_required
@_require_admin
def admin_group_delete(group_id):
    """Delete a group and its data (with backup, supports AJAX)"""
    result = admin_panel.admin_delete_group(group_id, current_user, backup_first=True)
    # If AJAX request, return JSON; else redirect
    if request.is_json or request.headers.get('Accept') == 'application/json':
        return jsonify(result)
    if result['ok']:
        flash(result['message'], 'success')
    else:
        flash(result['error'], 'danger')
    
    return redirect(url_for('admin_groups'))


@app.route("/admin/backups")
@login_required
@_require_admin
def admin_backups():
    """List available backups"""
    backups = admin_panel.admin_list_backups()
    # supply groups for restore dropdown
    groups = admin_panel.admin_list_all_groups()
    return render_template('admin/backups.html', backups=backups, groups=groups)

@app.route("/admin/backups/download/<backup_name>")
@login_required
@_require_admin
def admin_backup_download(backup_name):
    """Download a backup as a zip archive"""
    # Prevent path traversal by only allowing simple names (no slashes)
    if '/' in backup_name or '..' in backup_name:
        flash('Invalid backup name', 'danger')
        return redirect(url_for('admin_backups'))

    zip_path = admin_panel.admin_get_backup_zip(backup_name)
    if not zip_path or not os.path.exists(zip_path):
        flash('Backup not found or failed to create zip', 'danger')
        return redirect(url_for('admin_backups'))

    # send_file with as_attachment
    try:
        return send_file(zip_path, as_attachment=True)
    except Exception as e:
        logger.exception(f"Failed to send backup file: {e}")
        flash('Failed to download backup', 'danger')
        return redirect(url_for('admin_backups'))


@app.route("/admin/backups/restore/<path:backup_name>", methods=['POST'])
@login_required
@_require_admin
def admin_backup_restore(backup_name):
    # the legacy endpoint accepted bare filenames; we now also allow full
    # paths with leading slash (e.g. "/backups/tony/…") coming from the
    # remote-backup list.  strip any leading slash to avoid routing issues.
    backup_name = backup_name.lstrip('/')
    """Restore group from backup (supports AJAX)"""

    # parse group_id from either form-encoded or JSON payload; the
    # earlier implementation used ``request.json.get(..., type=int)`` which
    # failed because ``request.json`` returns a plain dict.
    group_id = None
    if request.is_json:
        try:
            payload = request.get_json(force=True, silent=True) or {}
        except Exception:
            payload = {}
        group_id = payload.get('group_id')
    else:
        group_id = request.form.get('group_id', type=int)

    try:
        if group_id is not None:
            group_id = int(group_id)
    except Exception:
        group_id = None

    # if this looks like a remote backup and we still lack a group,
    # attempt to guess from the path (second segment is usually folder name)
    if not group_id:
        if backup_name.startswith('backups/') or '/backups/' in backup_name:
            parts = backup_name.strip('/').split('/')
            if len(parts) >= 2:
                candidate = parts[1]
                from models import Group
                grp = Group.query.filter_by(data_folder=candidate).first()
                if grp:
                    group_id = grp.id
    # fallback: pick first group if available
    if not group_id:
        from models import Group
        first = Group.query.first()
        if first:
            group_id = first.id

    if not group_id:
        result = {'ok': False, 'error': 'Group ID required'}
        if request.is_json or request.headers.get('Accept') == 'application/json':
            return jsonify(result)
        flash('Group ID required', 'danger')
        return redirect(url_for('admin_backups'))

    result = admin_panel.admin_restore_backup(backup_name, group_id, current_user)
    if request.is_json or request.headers.get('Accept') == 'application/json':
        return jsonify(result)
    if result['ok']:
        flash(result['message'], 'success')
    else:
        flash(result['error'], 'danger')

    return redirect(url_for('admin_backups'))


@app.route("/admin/activity-logs")
@login_required
@_require_admin
def admin_activity_logs():
    """View activity logs (traffic tracking)"""
    logs = admin_panel.admin_get_activity_logs(limit=200)
    return render_template('admin/activity_logs.html', logs=logs)


@app.route('/admin/settings')
@login_required
@_require_admin
def admin_settings():
    settings = load_settings()
    return render_template('admin/settings.html', settings=settings)


@app.route('/admin/settings/save', methods=['POST'])
@login_required
@_require_admin
def admin_settings_save():
    form = request.form or {}
    # Global admin settings — stored in data/system/admin_settings.json
    settings = load_admin_settings()
    settings['site_title'] = form.get('site_title')
    
    # Save email provider setting
    email_provider = form.get('email_provider', '').strip()
    if email_provider in ['smtp', 'resend', 'oauth2_outlook', 'railway_proxy']:
        settings['email_provider'] = email_provider
    
    # Save railway proxy URL if provided
    railway_proxy_url = form.get('railway_proxy_url', '').strip()
    if railway_proxy_url:
        settings['railway_proxy_url'] = railway_proxy_url

    # Firebase backup sync policy
    sync_mode = str(form.get('firebase_backup_sync_mode') or 'login_logout').strip().lower()
    if sync_mode not in {'login_logout', 'scheduled'}:
        sync_mode = 'login_logout'
    settings['firebase_backup_sync_mode'] = sync_mode

    try:
        schedule_minutes = int(form.get('firebase_backup_schedule_minutes') or 30)
    except Exception:
        schedule_minutes = 30
    if schedule_minutes < 5:
        schedule_minutes = 5
    settings['firebase_backup_schedule_minutes'] = schedule_minutes
    
    save_admin_settings(settings)
    flash('Settings saved', 'success')
    return redirect(url_for('admin_settings'))


@app.route("/api/admin/users", methods=['GET'])
@login_required
def api_admin_users():
    """API endpoint for admin to list users (JSON)"""
    if not admin_panel.is_admin(current_user):
        return jsonify({'error': 'Admin access required'}), 403
    
    users = admin_panel.admin_list_all_users()
    return jsonify({'users': users})


@app.route("/api/admin/groups", methods=['GET'])
@login_required
def api_admin_groups():
    """API endpoint for admin to list groups (JSON)"""
    if not admin_panel.is_admin(current_user):
        return jsonify({'error': 'Admin access required'}), 403
    
    groups = admin_panel.admin_list_all_groups()
    return jsonify({'groups': groups})


@app.route("/api/admin/stats", methods=['GET'])
@login_required
def api_admin_stats():
    """API endpoint for system statistics"""
    if not admin_panel.is_admin(current_user):
        return jsonify({'error': 'Admin access required'}), 403
    
    stats = admin_panel.admin_get_system_stats()
    return jsonify(stats)


@app.route("/api/admin/activity-logs", methods=['GET'])
@login_required
def api_admin_activity_logs():
    """API endpoint for activity logs"""
    if not admin_panel.is_admin(current_user):
        return jsonify({'error': 'Admin access required'}), 403
    
    group_name = request.args.get('group')
    limit = request.args.get('limit', 100, type=int)
    
    logs = admin_panel.admin_get_activity_logs(group_name, limit)
    return jsonify({'logs': logs})


@app.route("/api/admin/firebase-usage", methods=["GET"])
@login_required
def api_admin_firebase_usage():
    """Return aggregated firebase.read bytes from activity.log for graphing.
    Query params:
      start (ISO8601) optional filter start time (UTC)
      end   (ISO8601) optional filter end time (UTC)
    Bundles points by hour.
    """
    if not admin_panel.is_admin(current_user):
        return jsonify({'error': 'Admin access required'}), 403

    start_raw = request.args.get('start')
    end_raw = request.args.get('end')
    start_ts = None
    end_ts = None
    try:
        if start_raw:
            start_ts = datetime.datetime.fromisoformat(start_raw)
            if start_ts.tzinfo is None:
                start_ts = start_ts.replace(tzinfo=timezone.utc)
            start_ts = start_ts.astimezone(timezone.utc)
        if end_raw:
            end_ts = datetime.datetime.fromisoformat(end_raw)
            if end_ts.tzinfo is None:
                end_ts = end_ts.replace(tzinfo=timezone.utc)
            end_ts = end_ts.astimezone(timezone.utc)
    except Exception:
        return jsonify({'error': 'Invalid start/end timestamps'}), 400

    log_path = os.path.join(os.getcwd(), 'data', 'activity.log')
    buckets = {}
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                if 'firebase.read' not in line:
                    continue
                # parse timestamp at beginning ISO format
                parts = line.split()
                if len(parts) < 2:
                    continue
                ts_str = parts[0] + ' ' + parts[1]
                try:
                    dt = datetime.datetime.fromisoformat(ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt = dt.astimezone(timezone.utc)
                except Exception:
                    continue
                if start_ts and dt < start_ts:
                    continue
                if end_ts and dt > end_ts:
                    continue
                m = re.search(r'size=(\d+)', line)
                if not m:
                    continue
                size = int(m.group(1))
                bucket = dt.strftime('%Y-%m-%d %H:00')
                buckets[bucket] = buckets.get(bucket, 0) + size
    # convert to sorted list for charting
    data = [{'ts': k, 'bytes': v} for k, v in sorted(buckets.items())]
    return jsonify({'success': True, 'data': data})


@app.route('/api/admin/firebase-sync-settings', methods=['GET', 'POST'])
@app.route('/admin/api/firebase-sync-settings', methods=['GET', 'POST'])
@login_required
def api_admin_firebase_sync_settings():
    """Get or update Firebase sync settings."""
    if not admin_panel.is_admin(current_user):
        return jsonify({'success': False, 'error': 'Admin access required'}), 403

    if request.method == 'GET':
        try:
            sync_cfg = utils.get_firebase_backup_sync_settings() or {}
        except Exception:
            logger.exception('Failed to read firebase sync settings; using defaults')
            sync_cfg = {}
        try:
            mode = str(sync_cfg.get('mode') or 'login_logout')
            if mode not in {'login_logout', 'scheduled'}:
                mode = 'login_logout'
            interval = int(sync_cfg.get('schedule_seconds') or 60)
            interval = max(10, min(3600, interval))
            smart_sync = bool(sync_cfg.get('smart_sync', True))
            enabled = (mode == 'scheduled')
            return jsonify({
                'success': True,
                'data': {
                    'mode': mode,
                    'enabled': enabled,
                    'interval': interval,
                    'schedule_unit': str(sync_cfg.get('schedule_unit') or 'seconds'),
                    'schedule_value': int(sync_cfg.get('schedule_value') or interval),
                    'smart_sync': smart_sync,
                    'schedule_minutes': int(sync_cfg.get('schedule_minutes') or max(5, int((interval + 59) // 60))),
                    'settings_source': str(sync_cfg.get('source') or '.env')
                }
            })
        except Exception:
            logger.exception('Failed to build firebase sync settings response; using hard defaults')
            return jsonify({
                'success': True,
                'data': {
                    'mode': 'login_logout',
                    'enabled': False,
                    'interval': 60,
                    'schedule_unit': 'seconds',
                    'schedule_value': 60,
                    'smart_sync': True,
                    'schedule_minutes': 5,
                    'settings_source': 'default'
                }
            })

    # POST: update settings
    try:
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get('mode') or '').strip().lower()
        if mode not in {'login_logout', 'scheduled'}:
            mode = 'scheduled' if bool(payload.get('enabled')) else 'login_logout'
        enabled = (mode == 'scheduled')
        schedule_unit = str(payload.get('schedule_unit') or 'seconds').strip().lower()
        if schedule_unit not in {'seconds', 'minutes', 'hours', 'days'}:
            schedule_unit = 'seconds'
        try:
            schedule_value = int(payload.get('schedule_value', payload.get('interval', 60)))
        except Exception:
            schedule_value = 60
        if schedule_value < 1:
            schedule_value = 1
        multiplier = {'seconds': 1, 'minutes': 60, 'hours': 3600, 'days': 86400}[schedule_unit]
        interval = schedule_value * multiplier
        interval = max(10, min(86400, interval))
        if schedule_unit == 'days':
            schedule_value = max(1, min(30, schedule_value))
            interval = schedule_value * 86400
        elif schedule_unit == 'hours':
            schedule_value = max(1, min(24, schedule_value))
            interval = schedule_value * 3600
        elif schedule_unit == 'minutes':
            schedule_value = max(1, min(1440, schedule_value))
            interval = schedule_value * 60
        else:
            schedule_value = max(10, min(3600, schedule_value))
            interval = schedule_value
        smart_sync = bool(payload.get('smart_sync', True))

        settings = load_admin_settings() or {}
        settings['firebase_backup_sync_mode'] = mode
        settings['firebase_backup_schedule_seconds'] = interval
        settings['firebase_backup_schedule_minutes'] = max(5, int((interval + 59) // 60))
        settings['firebase_backup_schedule_unit'] = schedule_unit
        settings['firebase_backup_schedule_value'] = schedule_value
        settings['firebase_smart_sync_enabled'] = smart_sync
        save_admin_settings(settings)
        
        # Update environment (in-memory and .env file)
        os.environ['FIREBASE_SYNC_MODE'] = mode
        os.environ['FIREBASE_SYNC_ENABLED'] = '1' if enabled else '0'
        os.environ['FIREBASE_SYNC_INTERVAL'] = str(interval)
        os.environ['FIREBASE_SYNC_UNIT'] = schedule_unit
        os.environ['FIREBASE_SYNC_VALUE'] = str(schedule_value)
        os.environ['FIREBASE_SMART_SYNC'] = '1' if smart_sync else '0'
        
        # Update .env file in the app root, not the current working directory.
        env_file = os.path.join(BASE_DIR, '.env')
        env_content = []
        if os.path.exists(env_file):
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if not any(line.startswith(k) for k in ['FIREBASE_SYNC_MODE=', 'FIREBASE_SYNC_ENABLED=', 'FIREBASE_SYNC_INTERVAL=', 'FIREBASE_SYNC_UNIT=', 'FIREBASE_SYNC_VALUE=', 'FIREBASE_SMART_SYNC=']):
                        env_content.append(line.rstrip('\n'))

        # Add/update settings
        env_content.append(f'FIREBASE_SYNC_MODE={mode}')
        env_content.append(f'FIREBASE_SYNC_ENABLED={"1" if enabled else "0"}')
        env_content.append(f'FIREBASE_SYNC_INTERVAL={interval}')
        env_content.append(f'FIREBASE_SYNC_UNIT={schedule_unit}')
        env_content.append(f'FIREBASE_SYNC_VALUE={schedule_value}')
        env_content.append(f'FIREBASE_SMART_SYNC={"1" if smart_sync else "0"}')

        with open(env_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(env_content) + '\n')
        
        logger.info(f'Firebase sync settings updated: mode={mode}, interval={interval}s, unit={schedule_unit}, value={schedule_value}, smart_sync={smart_sync}')
        # record activity for admin panel
        try:
            from utils import log_user_activity
            log_user_activity(
                user_id=current_user.id,
                group_name='system',
                action='firebase_sync_settings_updated',
                details={'mode': mode, 'enabled': enabled, 'interval': interval, 'schedule_unit': schedule_unit, 'schedule_value': schedule_value, 'smart_sync': smart_sync},
                user_email=getattr(current_user, 'email', None),
                user_username=getattr(current_user, 'username', None)
            )
        except Exception:
            pass
        
        return jsonify({'success': True, 'message': 'Settings saved', 'data': {
            'mode': mode,
            'enabled': enabled,
            'interval': interval,
            'schedule_unit': schedule_unit,
            'schedule_value': schedule_value,
            'smart_sync': smart_sync,
            'settings_source': 'process-env'
        }})
    except Exception as e:
        logger.error(f'Error saving firebase sync settings: {e}')
        return jsonify({'success': False, 'error': str(e)}), 400


@app.route('/api/admin/activity-logs/clear', methods=['POST'])
@login_required
def api_admin_activity_logs_clear():
    if not admin_panel.is_admin(current_user):
        return jsonify({'ok': False, 'error': 'Admin access required'}), 403

    payload = request.get_json(silent=True) or {}
    period = str(payload.get('period') or '').strip().lower()
    start_date = str(payload.get('start_date') or '').strip()
    end_date = str(payload.get('end_date') or '').strip()

    now = datetime.datetime.now(timezone.utc)
    cutoff_start = None
    cutoff_end = None

    def _sub_months(dt: datetime, months: int) -> datetime:
        y = dt.year
        m = dt.month - months
        while m <= 0:
            y -= 1
            m += 12
        import calendar
        d = min(dt.day, calendar.monthrange(y, m)[1])
        return dt.replace(year=y, month=m, day=d)

    try:
        if period in ('1m', '2m', '3m'):
            cutoff_start = _sub_months(now, int(period[0]))
        elif period == 'custom':
            if not start_date or not end_date:
                return jsonify({'ok': False, 'error': 'Απαιτούνται start_date και end_date για custom διάστημα.'}), 400
            cutoff_start = datetime.datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            cutoff_end = datetime.datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
            cutoff_end = cutoff_end.replace(hour=23, minute=59, second=59)
        else:
            return jsonify({'ok': False, 'error': 'Μη έγκυρο period. Επιτρεπτά: 1m, 2m, 3m, custom'}), 400
    except Exception:
        return jsonify({'ok': False, 'error': 'Μη έγκυρες ημερομηνίες.'}), 400

    def _parse_ts(ts: str):
        if not ts:
            return None
        variants = [str(ts).strip(), str(ts).strip().replace('Z', '+00:00')]
        for v in variants:
            try:
                dt = datetime.datetime.fromisoformat(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue
        return None

    def _should_delete(dt: datetime) -> bool:
        if not dt:
            return False
        if cutoff_end is not None:
            return cutoff_start <= dt <= cutoff_end
        return dt >= cutoff_start

    total_deleted = 0
    groups = Group.query.all()
    for grp in groups:
        folder = getattr(grp, 'data_folder', None) or grp.name

        # Firebase activity logs per group
        try:
            path = f'/activity_logs/{folder}'
            remote = firebase_config.firebase_read_data(path) or {}
            if isinstance(remote, dict):
                kept = {}
                for key, val in remote.items():
                    ts = _parse_ts((val or {}).get('timestamp') if isinstance(val, dict) else '')
                    if _should_delete(ts):
                        total_deleted += 1
                    else:
                        kept[key] = val
                firebase_config.firebase_delete_data(path)
                if kept:
                    firebase_config.firebase_write_data(path, kept)
        except Exception:
            current_app.logger.exception('Failed clearing Firebase activity logs for %s', folder)

        # Local JSONL/plain file fallback logs
        try:
            group_dir = os.path.join(os.getcwd(), 'data', str(folder))
            for file_name in ('activity.log.jsonl', 'activity.log'):
                fpath = os.path.join(group_dir, file_name)
                if not os.path.exists(fpath):
                    continue
                kept_lines = []
                with open(fpath, 'r', encoding='utf-8') as fh:
                    for line in fh:
                        line_stripped = line.strip()
                        if not line_stripped:
                            continue
                        parsed = None
                        ts = None
                        try:
                            parsed = json.loads(line_stripped)
                            if isinstance(parsed, dict):
                                ts = _parse_ts(parsed.get('timestamp') or '')
                        except Exception:
                            parsed = None
                        if ts is None and ' - ' in line_stripped:
                            ts = _parse_ts(line_stripped.split(' - ', 1)[0])
                        if _should_delete(ts):
                            total_deleted += 1
                            continue
                        kept_lines.append(line)
                with open(fpath, 'w', encoding='utf-8') as fw:
                    fw.writelines(kept_lines)
        except Exception:
            current_app.logger.exception('Failed clearing local activity logs for %s', folder)

    try:
        firebase_config.firebase_log_activity(str(getattr(current_user, 'id', 'admin')), 'admin', 'activity_logs_cleared', {
            'period': period,
            'start_date': start_date,
            'end_date': end_date,
            'deleted_entries': total_deleted,
        })
    except Exception:
        pass

    return jsonify({'ok': True, 'deleted_entries': total_deleted})


@app.route("/admin/send-email", methods=['GET', 'POST'])
@login_required
@_require_admin
def admin_send_email():
    """Admin: send email to selected users"""
    if request.method == 'GET':
        users = admin_panel.admin_list_all_users()
        return render_template('admin/send_email.html', users=users)
    
    # POST: send email
    try:
        from email_utils import send_bulk_email_to_users
        
        user_ids = request.form.getlist('user_ids')
        subject = request.form.get('subject', '').strip()
        message = request.form.get('message', '').strip()
        
        if not user_ids or not subject or not message:
            flash('Please select users and provide subject and message', 'danger')
            return redirect(url_for('admin_send_email'))
        
        user_ids = [int(uid) for uid in user_ids]
        
        # Build HTML body
        html_body = f"""
        <html>
            <body>
                <h3>{subject}</h3>
                <hr>
                <div style="white-space: pre-wrap; line-height: 1.6;">
                    {message}
                </div>
                <hr>
                <p><small>This is a message from the Firebed Admin Team</small></p>
            </body>
        </html>
        """
        
        result = send_bulk_email_to_users(user_ids, subject, html_body)
        
        flash(f'Email sent to {result["sent"]} users; {result["failed"]} failed', 'success' if result['failed'] == 0 else 'warning')
        if result['errors']:
            current_app.logger.warning(f"Email send errors: {result['errors']}")
        
        return redirect(url_for('admin_send_email'))
    
    except Exception as e:
        logger.exception('Failed to send bulk email')
        flash(f'Error sending emails: {str(e)}', 'danger')
        return redirect(url_for('admin_send_email'))


_firebase_backup_scheduler_started = False
_firebase_backup_scheduler_lock = threading.Lock()
_firebase_backup_last_run: Dict[str, float] = {}


def _read_group_sync_settings_by_folder(group_folder: str) -> Dict[str, Any]:
    try:
        p = os.path.join(BASE_DIR, 'data', group_folder, 'credentials_settings.json')
        if os.path.exists(p):
            with open(p, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception:
        log.exception('Failed reading group sync settings for folder=%s', group_folder)
    return {}


def _firebase_backup_scheduler_loop():
    while True:
        try:
            with app.app_context():
                sync_cfg = utils.get_firebase_backup_sync_settings() or {}
                mode = str(sync_cfg.get('mode') or 'login_logout').strip().lower()
                interval_secs = int(sync_cfg.get('schedule_seconds') or 60)
                if interval_secs < 10:
                    interval_secs = 10
                if mode != 'scheduled':
                    time.sleep(30)
                    continue

                groups = Group.query.all() or []
                now_ts = time.time()
                for grp in groups:
                    try:
                        group_name = str(getattr(grp, 'name', '') or '').strip()
                        group_folder = str(getattr(grp, 'data_folder', '') or '').strip()
                        if not group_name or not group_folder:
                            continue

                        prev = float(_firebase_backup_last_run.get(group_name) or 0)
                        if prev and (now_ts - prev) < interval_secs:
                            continue

                        log.info('Scheduled Firebase backup sync start for group=%s (interval=%ss)', group_name, interval_secs)
                        # Keep local payload ready, then reconcile based on which side is newer.
                        try:
                            firebase_config.ensure_group_data_local(group_folder, create_empty_dirs=True)
                        except Exception:
                            log.exception('Scheduled bootstrap check failed for group=%s', group_name)

                        sync_result = True
                        try:
                            freshness = firebase_config.compare_group_payload_freshness(
                                group_name,
                                local_group_folder=group_folder,
                                local_data_root=os.path.join(os.getcwd(), 'data')
                            )
                            action = str(freshness.get('action') or 'unknown')
                            reason = str(freshness.get('reason') or '')

                            if action == 'pull':
                                log.info('Scheduled Firebase reconcile chose PULL for group=%s reason=%s', group_name, reason)
                                sync_result = bool(firebase_config.firebase_pull_group_to_local(
                                    group_name,
                                    local_data_root=os.path.join(os.getcwd(), 'data'),
                                    force=True,
                                    local_group_folder=group_folder
                                ))
                            elif action == 'push':
                                log.info('Scheduled Firebase reconcile chose PUSH for group=%s reason=%s', group_name, reason)
                                sync_result = bool(firebase_config.firebase_push_group_files(
                                    group_name,
                                    local_data_root=os.path.join(os.getcwd(), 'data'),
                                    dry_run=False,
                                    verbose=False,
                                    force=True,
                                    local_group_folder=group_folder
                                ))
                            else:
                                log.info('Scheduled Firebase reconcile chose incremental PUSH for group=%s reason=%s', group_name, reason)
                                sync_result = bool(firebase_config.firebase_push_group_files(
                                    group_name,
                                    local_data_root=os.path.join(os.getcwd(), 'data'),
                                    dry_run=False,
                                    verbose=False,
                                    force=False,
                                    local_group_folder=group_folder
                                ))
                        except Exception:
                            log.exception('Scheduled freshness reconcile failed for group=%s', group_name)
                            sync_result = False

                        _firebase_backup_last_run[group_name] = time.time()
                        log.info('Scheduled Firebase backup sync done for group=%s sync_ok=%s', group_name, sync_result)
                    except Exception:
                        log.exception('Scheduled Firebase backup sync failed for group=%s', getattr(grp, 'name', None))
        except Exception:
            log.exception('Firebase backup scheduler loop error')
        time.sleep(30)


def _start_firebase_backup_scheduler_once():
    global _firebase_backup_scheduler_started
    with _firebase_backup_scheduler_lock:
        if _firebase_backup_scheduler_started:
            return
        t = threading.Thread(target=_firebase_backup_scheduler_loop, daemon=True, name='firebase-backup-scheduler')
        t.start()
        _firebase_backup_scheduler_started = True
        log.info('Firebase backup scheduler thread started')


try:
    _is_reloader_main = (os.environ.get('WERKZEUG_RUN_MAIN') == 'true')
    _debug_mode = bool(app.debug or os.getenv('FLASK_DEBUG', '0') == '1')
    if (not _debug_mode) or _is_reloader_main:
        _start_firebase_backup_scheduler_once()
except Exception:
    log.exception('Failed to start firebase backup scheduler thread')


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    debug_flag = True
    app.run(host="0.0.0.0", port=port, debug=debug_flag, use_reloader=True)
