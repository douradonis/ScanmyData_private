#!/usr/bin/env python3
import re
import base64
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, parse_qs, unquote, urlencode
import xml.etree.ElementTree as ET
import os

# attempt to load a .env file if present so that environment variables can be
# configured via that file; repeated import will be idempotent.
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except Exception:
    pass

# helper to decide whether browser fallback is permitted; evaluated each time
# so that changes to the environment (including via reloading a .env file)
# take effect without restarting the interpreter.
def _use_browser_fallback() -> bool:
    if os.getenv("MYDATA_USE_BROWSER", "0").lower() in ("1", "true", "yes"):
        return True
    return False


def _provider_needs_browser_fallback(url: str) -> bool:
    """Known providers where the MyData action is often JS-only."""
    try:
        domain = (urlparse(str(url)).netloc or "").lower()
    except Exception:
        return False
    return any(d in domain for d in ("e-invoicing.gr", "einvoice.s1ecos.gr", "s1ecos.gr"))

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "el-GR,el;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://mydata.wedoconnect.com/",
    "DNT": "1",
}
MARK_RE = re.compile(r"\b\d{15}\b")  # 15-digit MARK
VAT_RE = re.compile(r"\b\d{9}\b")    # 9-digit AFM

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


def _resolve_mydatapi_via_browser(url, timeout=20, debug=False):
    """
    JS-aware fallback: ανοίγει τη σελίδα και προσπαθεί να πατήσει το κουμπί
    "Προβολή μέσω MyData" για να πιάσει το τελικό mydatapi URL.

    This operation launches a full Chromium instance which consumes hundreds
    of megabytes of RAM.  On constrained environments (Render free tier, CI
    containers, etc.) this often exceeds the memory quota.  The behaviour is
    controlled by the ``MYDATA_USE_BROWSER`` environment variable; when it is
    false (the default) the function simply returns ``None`` immediately.
    """
    if not _use_browser_fallback() and not _provider_needs_browser_fallback(url):
        if debug:
            print("browser fallback disabled via MYDATA_USE_BROWSER")
        return None

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    timeout_ms = int(max(timeout, 8) * 1000)
    candidates = [url + ("&" if "?" in url else "?") + "peppol=true", url]
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    found = {"url": None}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()

            def _capture_request(req):
                ru = req.url
                if "mydatapi.aade.gr" in ru and "TimologioQR/QRInfo" in ru:
                    found["url"] = ru

            page.on("request", _capture_request)

            for cu in candidates:
                try:
                    page.goto(cu, wait_until="networkidle", timeout=timeout_ms)
                    page.wait_for_timeout(4500)
                except Exception:
                    continue

                # try direct extraction again after JS render
                rendered = page.content()
                rendered_url = _extract_mydatapi_url_from_text(rendered, page.url)
                if rendered_url:
                    browser.close()
                    return rendered_url

                if found["url"]:
                    browser.close()
                    return found["url"]
                if "mydatapi.aade.gr" in page.url and "TimologioQR/QRInfo" in page.url:
                    browser.close()
                    return page.url

                clicked = False

                # 1) target buttons whose runtime text contains MyData
                buttons = page.locator("button")
                btn_count = min(buttons.count(), 40)
                for i in range(btn_count):
                    btn = buttons.nth(i)
                    try:
                        text = (btn.inner_text(timeout=1000) or "").strip().lower()
                    except Exception:
                        continue
                    if "mydata" not in text and "my data" not in text:
                        continue
                    try:
                        with page.expect_popup(timeout=5000) as popinfo:
                            btn.click(timeout=5000)
                        pop = popinfo.value
                        try:
                            pop.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                        except Exception:
                            pass
                        if "mydatapi.aade.gr" in pop.url and "TimologioQR/QRInfo" in pop.url:
                            browser.close()
                            return pop.url
                    except Exception:
                        try:
                            btn.click(timeout=5000)
                            clicked = True
                        except Exception:
                            continue

                # 2) fallback selectors
                for sel in ["a:has-text('MyData')", "button.opButton"]:
                    loc = page.locator(sel)
                    if loc.count() <= 0:
                        continue
                    try:
                        with page.expect_popup(timeout=3500) as popinfo:
                            loc.first.click(timeout=3500)
                        pop = popinfo.value
                        if "mydatapi.aade.gr" in pop.url and "TimologioQR/QRInfo" in pop.url:
                            browser.close()
                            return pop.url
                    except Exception:
                        try:
                            loc.first.click(timeout=3500)
                            clicked = True
                        except Exception:
                            continue

                if clicked:
                    try:
                        page.wait_for_timeout(1800)
                    except Exception:
                        pass

                if found["url"]:
                    browser.close()
                    return found["url"]
                if "mydatapi.aade.gr" in page.url and "TimologioQR/QRInfo" in page.url:
                    browser.close()
                    return page.url

            browser.close()
    except Exception as e:
        if debug:
            print("megasoft browser fallback error:", e)

    return found["url"]


# -------------------- WEDOCONNECT --------------------
# -------------------- WEDOCONNECT --------------------
def _find_erp_qr_target(soup, base_url):
    """
    Βρίσκει τον τελικό target URL του κουμπιού #erpQrBtn (ή παραλλαγές),
    ακόμη κι αν είναι σε onclick, data-href κλπ. Επιστρέφει absolute URL ή None.
    """
    # 1) Άμεσο στοιχείο με id=erpQrBtn
    el = soup.find(id="erpQrBtn")
    href = None
    if el:
        href = el.get("href") or el.get("data-href") or el.get("data-url")
        if not href:
            onclick = el.get("onclick") or ""
            m = re.search(r"(?:window\.open|open|location\.href)\(\s*['\"]([^'\"]+)['\"]", onclick)
            if m:
                href = m.group(1)

    # 2) Εναλλακτικές (anchor/button με class ή id)
    if not href:
        a = soup.select_one("a#erpQrBtn, a.erpQrBtn, button#erpQrBtn, button.erpQrBtn")
        if a and a.get("href"):
            href = a["href"]

    # 3) Οποιοδήποτε <a> που δείχνει ήδη σε mydatapi
    if not href:
        for a in soup.find_all("a", href=True):
            if "mydatapi.aade.gr" in a["href"]:
                href = a["href"]
                break

    # 4) Generic MyData buttons/links (σελίδες χωρίς erpQrBtn)
    if not href:
        for a in soup.find_all("a", href=True):
            a_href = (a.get("href") or "").strip()
            a_txt = (a.get_text(" ", strip=True) or "").lower()
            if not a_href:
                continue
            if (
                "mydata" in a_txt
                or "my data" in a_txt
                or "timologioqr/qrinfo" in a_href.lower()
                or ("mydata" in a_href.lower() and "qrinfo" in a_href.lower())
            ):
                href = a_href
                break
    if not href:
        for b in soup.find_all("button"):
            b_txt = (b.get_text(" ", strip=True) or "").lower()
            if "mydata" not in b_txt and "my data" not in b_txt:
                continue
            b_href = (b.get("data-href") or b.get("data-url") or "").strip()
            if b_href:
                href = b_href
                break
            onclick = (b.get("onclick") or "")
            m_btn = re.search(r"(?:window\.open|open|location\.href)\(\s*['\"]([^'\"]+)['\"]", onclick)
            if m_btn:
                href = m_btn.group(1)
                break

    # 5) Αναζήτηση μέσα σε <script> (hard fallback)
    if not href:
        for script in soup.find_all("script"):
            sc = (script.string or script.get_text() or "")
            m = re.search(r"https?://mydatapi\.aade\.gr[^\s\"']+", sc)
            if m:
                href = m.group(0)
                break
            m2 = re.search(r"(?:/|https?://[^\s\"']+)TimologioQR/QRInfo\?q=[^\s\"']+", sc, re.I)
            if m2:
                href = m2.group(0)
                break

    if not href:
        return None
    return urljoin(base_url, href)


def _erp_qr_to_mydatapi_from_soup(sess, soup, base_url, timeout=15):
    """
    Από ήδη φορτωμένη σελίδα: βρίσκει το erpQrBtn, κάνει follow, και διαβάζει το mydatapi.
    Επιστρέφει dict από scrape_mydatapi ή None.
    """
    target = _find_erp_qr_target(soup, base_url)
    if not target:
        return None

    r2 = sess.get(target, timeout=timeout, allow_redirects=True)
    r2.raise_for_status()

    final_url = r2.url
    if "mydatapi.aade.gr" not in urlparse(final_url).netloc:
        # Προσπάθησε να εξάγεις mydatapi URL από τη σελίδα (meta refresh / link)
        soup2 = BeautifulSoup(r2.text, "html.parser")
        a2 = soup2.find("a", href=lambda h: h and "mydatapi.aade.gr" in h)
        if a2:
            final_url = urljoin(r2.url, a2["href"])
        else:
            meta = soup2.find("meta", attrs={"http-equiv": lambda v: v and v.lower() == "refresh"})
            if meta and meta.get("content"):
                cm = re.search(r"url=([^;]+)", meta["content"], re.I)
                if cm:
                    final_url = urljoin(r2.url, cm.group(1).strip())

    data = scrape_mydatapi(final_url)
    if data:
        data["__mydatapi_url__"] = final_url
    return data

def _extract_from_root(root):
    """
    Δομημένη εξαγωγή από ElementTree root (namespace-agnostic).
    Επιστρέφει (marks_list, counterpart_vat or None)
    """
    marks = []
    for adr in root.findall(".//{*}AdditionalDocumentReference"):
        id_el = adr.find(".//{*}ID")
        desc_el = adr.find(".//{*}DocumentDescription")
        if id_el is not None and id_el.text:
            desc_text = (desc_el.text or "").upper() if desc_el is not None else ""
            if "M.AR.K" in desc_text or "MARK" in desc_text:
                marks.append(id_el.text.strip())

    counterpart_vat = None
    candidate_containers = (
        root.findall(".//{*}AccountingCustomerParty") +
        root.findall(".//{*}counterpart") +
        root.findall(".//{*}CounterParty")
    )
    for cont in candidate_containers:
        for el in cont.iter():
            ln = el.tag.split("}")[-1].lower()
            if ln in ("companyid", "vatnumber"):
                txt = (el.text or "").strip()
                m = VAT_RE.search(txt)
                if m:
                    counterpart_vat = m.group(0)
                    break
        if counterpart_vat:
            break

    if not counterpart_vat:
        for el in root.iter():
            txt = (el.text or "").strip()
            if txt:
                m = VAT_RE.search(txt)
                if m:
                    counterpart_vat = m.group(0)
                    break

    seen = set()
    marks = [m for m in marks if not (m in seen or seen.add(m))]
    return marks, counterpart_vat


def scrape_wedoconnect(url, timeout=20, debug=False):
    """
    Επιστρέφει (marks_list, counterpart_vat)
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)

    try:
        r = sess.get(url, timeout=timeout)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        if debug: print("[RequestError page]", e)
        return [], None

    soup = BeautifulSoup(html, "html.parser")
    candidate_urls = []

    # anchors
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(r.url, href)
        low = href.lower()
        txt = (a.get("title") or a.get("download") or a.text or "").lower()
        if any(tok in low for tok in (".xml", "az-ubl", "az_ubl", "ubl", "mydatafilecontainer", "mydata")):
            candidate_urls.append(full)
        elif any(tok in txt for tok in ("az-ubl", "mydata", "ubl", ".xml")):
            candidate_urls.append(full)

    # iframe / embed / inline XML links
    for iframe in soup.find_all("iframe", src=True):
        candidate_urls.append(urljoin(r.url, iframe["src"]))
    for emb in soup.find_all("embed", src=True):
        candidate_urls.append(urljoin(r.url, emb["src"]))
    for m in re.findall(r'https?://[^\s"\'<>]+(?:\.xml|az-ubl|az_ubl|mydatafilecontainer|blob\.core\.windows\.net)[^\s"\'<>]*', html, flags=re.I):
        candidate_urls.append(m)

    seen = set()
    candidate_urls = [u for u in candidate_urls if not (u in seen or seen.add(u))]

    page_marks = MARK_RE.findall(html)
    marks = list(dict.fromkeys(page_marks))
    counterpart_vat = None

    for cu in candidate_urls:
        if debug: print("[debug] trying candidate:", cu)
        try:
            r2 = sess.get(cu, timeout=timeout)
            r2.raise_for_status()
            content = r2.content
            text = r2.text
        except Exception as e:
            if debug: print("[debug] candidate fetch failed:", e)
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
            marks_xml, vat_xml = _extract_from_root(root)
            for m in marks_xml:
                if m not in marks:
                    marks.append(m)
            if vat_xml:
                counterpart_vat = vat_xml
            if marks or counterpart_vat:
                return marks, counterpart_vat
            m_mark = MARK_RE.search(text)
            m_vat = VAT_RE.search(text)
            marks_f = [m_mark.group(0)] if m_mark else []
            vat_f = m_vat.group(0) if m_vat else None
            if marks_f or vat_f:
                return list(dict.fromkeys(marks + marks_f)), vat_f
            continue

        m2 = re.search(rb"\b\d{9}\b", content)
        if m2:
            counterpart_vat = m2.group(0).decode("ascii")
            return marks, counterpart_vat
        m3 = re.search(r"\b\d{9}\b", text)
        if m3:
            counterpart_vat = m3.group(0)
            return marks, counterpart_vat

    if not counterpart_vat:
        page_vat = VAT_RE.search(html)
        if page_vat:
            counterpart_vat = page_vat.group(0)

    marks = list(dict.fromkeys(marks))
    return marks, counterpart_vat


# -------------------- MYDATAPI --------------------
def scrape_mydatapi(url, debug=False):
    """
    Επιστρέφει dict όπως προηγουμένως: MARK, Είδος Παραστατικού, ΑΦΜ Πελάτη
    Τα δεδομένα είναι σε JavaScript variables, όχι σε HTML inputs.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.encoding = 'utf-8'
        r.raise_for_status()
    except Exception as e:
        if debug:
            print(f"[RequestError] {e}")
        return {}

    html = r.text
    
    # Τα δεδομένα είναι σε JavaScript variables μέσα σε <script> tags
    # Ψάχνουμε για patterns όπως: var mark = "400011490687468";
    mark = None
    doc_type = None
    afm = None
    
    # Pattern 1: JavaScript variable assignments
    mark_match = re.search(r'(?:var\s+)?(?:mark|tmark|MARK)\s*[=:]\s*["\']([0-9]{15})["\']', html, re.I)
    if mark_match:
        mark = mark_match.group(1)
    
    # Pattern 2: Direct 15-digit number in scripts (fallback)
    if not mark:
        for script in BeautifulSoup(html, "html.parser").find_all("script"):
            script_text = script.string or script.get_text() or ""
            m = re.search(r'\b([0-9]{15})\b', script_text)
            if m:
                mark = m.group(1)
                break
    
    # Pattern 3: ΑΦΜ Πελάτη - ψάχνουμε πρώτα για τo crvatnumber field (customer VAT)
    # Αυτό είναι το πιο αξιόπιστο, γιατί έχει id="crvatnumber"
    crvatnumber_input = BeautifulSoup(html, "html.parser").find("input", id="crvatnumber")
    if crvatnumber_input and crvatnumber_input.get("value"):
        afm = crvatnumber_input.get("value").strip()
    
    # Pattern 4: Fallback - ψάξε για crvatnumber, vatNumber, counterpartVat, κλπ σε plain text
    if not afm:
        afm_match = re.search(r'(?:var\s+)?(?:crvatnumber|vatNumber|counterpartVat|afm)\s*[=:]\s*["\']?([0-9]{9})["\']?', html, re.I)
        if afm_match:
            afm = afm_match.group(1)
    
    # Pattern 5: Είδος Παραστατικού
    dtype_match = re.search(r'(?:var\s+)?(?:dtype|docType|invoiceType)\s*[=:]\s*["\']([^"\']+)["\']', html, re.I)
    if dtype_match:
        doc_type = dtype_match.group(1)
    
    # Fallback: Αν δεν βρήκαμε τίποτα, ψάξε για οποιοδήποτε 15ψήφιο και 9ψήφιο αριθμό
    if not mark:
        m = re.search(r'\b([0-9]{15})\b', html)
        if m:
            mark = m.group(1)
    
    if not afm:
        # Βρες όλα τα 9ψήφια και πάρε το δεύτερο (συνήθως είναι του πελάτη, όχι του εκδότη)
        all_vats = re.findall(r'\b([0-9]{9})\b', html)
        if len(all_vats) >= 2:
            afm = all_vats[1]  # Δεύτερο είναι συνήθως ο πελάτης
        elif all_vats:
            afm = all_vats[0]
    
    return {
        "MARK": mark.strip() if mark else "N/A",
        "Είδος Παραστατικού": doc_type.strip() if doc_type else "N/A",
        "ΑΦΜ Πελάτη": afm.strip() if afm else "N/A"
    }


# -------------------- ECOS E-INVOICING --------------------
def scrape_einvoice(url):
    """
    Επιστρέφει (mark, counterpart_vat)
    - Αν υπάρχει #erpQrBtn που οδηγεί σε mydatapi, παίρνει MARK/ΑΦΜ από scrape_mydatapi (προτιμητέο).
    - Αλλιώς, συνεχίζει με την υπάρχουσα λογική εξαγωγής (πίνακες/attachments).
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        r = sess.get(url, timeout=15)
        r.raise_for_status()
        r.encoding = 'utf-8'
    except Exception as e:
        print(f"[RequestError] {e}")
        return None, None

    soup = BeautifulSoup(r.text, "html.parser")

    # 1) Προσπάθησε μέσω erpQrBtn -> mydatapi
    try:
        data = _erp_qr_to_mydatapi_from_soup(sess, soup, r.url, timeout=15)
    except Exception:
        data = None

    if not data:
        # Some S1ECOS pages expose the TimologioQR URL directly in HTML/JS.
        direct_mydatapi = _extract_mydatapi_url_from_text(r.url, r.url) or _extract_mydatapi_url_from_text(r.text, r.url)
        if direct_mydatapi:
            try:
                data = scrape_mydatapi(direct_mydatapi)
            except Exception:
                data = None

    if not data:
        # JS-only buttons may require a browser to reveal/follow the MyData URL.
        try:
            resolved = _resolve_mydatapi_via_browser(r.url, timeout=20)
            if resolved:
                data = scrape_mydatapi(resolved)
        except Exception:
            data = None

    if data:
        mark = (data.get("MARK") or "").strip()
        afm = (data.get("ΑΦΜ Πελάτη") or "").strip()
        afm = re.sub(r"\D", "", afm) if afm else None
        mark_str = mark if mark and mark != "N/A" else None
        marks = [mark_str] if mark_str else []
        return marks, afm

    # 2) Fallback στην παλιά λογική
    # 1) MARK extraction
    mark = None
    mark_tag = soup.find("span", class_=lambda c: c and "field-Mark" in c)
    if mark_tag:
        val = mark_tag.find("span", class_="value")
        if val and val.get_text(strip=True):
            mark = val.get_text(strip=True)
    if not mark:
        txt = soup.get_text(" ", strip=True)
        m = re.search(r"\b\d{15}\b", txt)
        if m:
            mark = m.group(0)

    counterpart_vat = None

    # 2) mydatalogo link (παλιό heuristic)
    mydatapi_candidate = None
    for a in soup.find_all("a", href=True):
        img = a.find("img")
        if img and img.get("src"):
            src = img["src"].lower()
            if "mydatalogo" in src or "mydatlogo" in src or ("mydata" in src and "logo" in src):
                mydatapi_candidate = urljoin(r.url, a["href"])
                break
    if not mydatapi_candidate:
        for img in soup.find_all("img", src=True):
            src = img["src"].lower()
            if "mydatalogo" in src or "mydatlogo" in src or ("mydata" in src and "logo" in src):
                parent_a = img.find_parent("a")
                if parent_a and parent_a.get("href"):
                    mydatapi_candidate = urljoin(r.url, parent_a["href"])
                    break
    if mydatapi_candidate:
        try:
            r2 = sess.get(mydatapi_candidate, timeout=15)
            r2.raise_for_status()
            resolved_url = r2.url
            mydata_info = scrape_mydatapi(resolved_url)
            afm = mydata_info.get("ΑΦΜ Πελάτη") or mydata_info.get("ΑΦΜ", "")
            if afm and afm != "N/A":
                counterpart_vat = re.sub(r"\D", "", afm)
        except Exception:
            counterpart_vat = None

    # 3) ...ό,τι είχες ήδη για εύρεση ΑΦΜ (κρατημένο όπως πριν)...
    if not counterpart_vat:
        heading = soup.find(string=re.compile(r"ΣΤΟΙΧΕΙΑ\s+ΑΝΤΙΣΥΜΒΑΛΛΟΜΕΝΟΥ|ΣΤΟΙΧΕΙΑ\s+ΠΕΛΑΤΗ", re.I))
        if heading:
            ancestor = heading.find_parent()
            table = None
            if ancestor:
                table = ancestor.find_parent("table") or ancestor.find_next("table")
            if table:
                for tr in table.find_all("tr"):
                    tr_text = tr.get_text(" ", strip=True)
                    m = re.search(r"Α\.?Φ\.?Μ\.?[:\s]*([0-9]{9})", tr_text, flags=re.I)
                    if m:
                        counterpart_vat = m.group(1)
                        break
                    tds = tr.find_all("td")
                    if tds:
                        last_txt = tds[-1].get_text(" ", strip=True)
                        m2 = re.search(r"([0-9]{9})", last_txt)
                        if m2 and ("ΑΦΜ" in tr_text.upper() or "Α.Φ.Μ." in tr_text.upper()):
                            counterpart_vat = m2.group(1)
                            break

    if not counterpart_vat:
        cp_section = soup.find("div", class_=lambda c: c and "section-counterparties" in c)
        if cp_section:
            for td in cp_section.find_all("td"):
                txt = (td.get_text(" ", strip=True) or "")
                m = re.search(r"Α\.?Φ\.?Μ\.?[:\s]*([0-9]{9})", txt, flags=re.I)
                if m:
                    counterpart_vat = m.group(1)
                    break
            if not counterpart_vat:
                for tr in cp_section.find_all("tr"):
                    tr_text = tr.get_text(" ", strip=True)
                    if "Α.Φ.Μ." in tr_text or "ΑΦΜ" in tr_text.upper():
                        tds = tr.find_all("td")
                        if tds:
                            last_txt = tds[-1].get_text(" ", strip=True)
                            m2 = re.search(r"([0-9]{9})", last_txt)
                            if m2:
                                counterpart_vat = m2.group(1)
                                break

    if not counterpart_vat:
        candidate_urls = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            low = href.lower()
            if any(tok in low for tok in (".xml", "az-ubl", "ubl", "mydatafilecontainer", "mydata")) or "blob.core.windows.net" in low:
                candidate_urls.append(urljoin(r.url, href))
        for iframe in soup.find_all("iframe", src=True):
            src = iframe["src"]
            fullsrc = urljoin(r.url, src)
            parsed = urlparse(fullsrc)
            qd = parse_qs(parsed.query)
            if "file" in qd:
                candidate_urls.append(unquote(qd["file"][0]))
            candidate_urls.append(fullsrc)
        seen = set()
        candidate_urls = [u for u in candidate_urls if not (u in seen or seen.add(u))]
        for cu in candidate_urls:
            try:
                r3 = sess.get(cu, timeout=15)
                r3.raise_for_status()
                text = r3.text
                content = r3.content
            except Exception:
                continue
            if "xml" in (r3.headers.get("Content-Type") or "").lower() or "<?xml" in text[:200] or re.search(r"<(Invoice|InvoicesDoc|cbc:Invoice)", text, flags=re.I):
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
                el = root.find(".//{*}AccountingCustomerParty") or root.find(".//{*}counterpart") or root.find(".//counterpart")
                if el is not None:
                    comp = el.find(".//{*}CompanyID") or el.find(".//{*}vatNumber") or el.find(".//vatNumber")
                    if comp is not None and (comp.text or "").strip():
                        m = re.search(r"(\d{9})", comp.text)
                        if m:
                            counterpart_vat = m.group(1)
                            break
                m_any = re.search(r"EL?([0-9]{9})", text)
                if m_any:
                    counterpart_vat = m_any.group(1)
                    break
            else:
                m2 = re.search(rb"\b\d{9}\b", content)
                if m2:
                    counterpart_vat = m2.group(0).decode("ascii")
                    break
                m3 = re.search(r"\b\d{9}\b", text)
                if m3:
                    counterpart_vat = m3.group(0)
                    break

    if not counterpart_vat:
        page_txt = soup.get_text(" ", strip=True)
        m = re.search(r"Α\.?Φ\.?Μ\.?[:\s]*([0-9]{9})", page_txt, flags=re.I)
        if m:
            counterpart_vat = m.group(1)
        else:
            m2 = re.search(r"\b([0-9]{9})\b", page_txt)
            if m2:
                url_match = re.search(r"/v/EL?(\d{9})[-_]", url, flags=re.I)
                if url_match:
                    url_v = url_match.group(1)
                    all9 = re.findall(r"\b([0-9]{9})\b", page_txt)
                    for a9 in all9:
                        if a9 != url_v:
                            counterpart_vat = a9
                            break
                    if not counterpart_vat and all9:
                        counterpart_vat = all9[0]
                else:
                    counterpart_vat = m2.group(1)

    if counterpart_vat:
        counterpart_vat = re.sub(r"\D", "", counterpart_vat)

    # ΕΠΙΣΤΡΟΦΗ: marks είναι LIST, counterpart_vat είναι string
    marks = [mark] if mark else []
    return marks, counterpart_vat

# -------------------- IMPACT E-INVOICING --------------------
def scrape_impact(url):
    """
    Επιστρέφει (mark, counterpart_vat) — όπου mark είναι str ή None.
    1) Αν υπάρχει embedded mydatapi URL → διαβάζει MARK/ΑΦΜ από scrape_mydatapi.
    2) Αλλιώς fallback στην παλιά εξαγωγή του MARK μόνο.
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        r = sess.get(url, headers=HEADERS, timeout=15)
        r.encoding = 'utf-8'
        r.raise_for_status()
    except Exception as e:
        print(f"[RequestError] {e}")
        return None, None

    soup = BeautifulSoup(r.text, "html.parser")

    # 1) Ψάξε για embedded mydatapi URL στο HTML
    mydatapi_url = _extract_mydatapi_url_from_text(r.text, r.url)
    if mydatapi_url:
        try:
            data = scrape_mydatapi(mydatapi_url, debug=False)
            if data:
                mark = (data.get("MARK") or "").strip()
                afm = (data.get("ΑΦΜ Πελάτη") or "").strip()
                afm = re.sub(r"\D", "", afm) if afm else None
                mark_str = mark if mark and mark != "N/A" else None
                if mark_str or afm:
                    return mark_str, afm
        except Exception:
            pass

    # 2) Fallback: παλιά λογική εύρεσης MARK από τη σελίδα
    el = soup.select_one("span.field.field-Mark span.value, span.field-Mark span.value")
    if el and el.get_text(strip=True):
        return el.get_text(strip=True), None

    for lbl in soup.find_all(string=re.compile(r"Μ\.?Αρ\.?Κ\.?", re.I)):
        parent = lbl.parent
        if parent:
            block_text = parent.get_text(" ", strip=True)
            m = MARK_RE.search(block_text)
            if m:
                return m.group(0), None
            sib_text = " ".join(
                str(sib.get_text(" ", strip=True) if hasattr(sib, "get_text") else sib)
                for sib in parent.next_siblings
            )
            m2 = MARK_RE.search(sib_text)
            if m2:
                return m2.group(0), None

    full_text = soup.get_text(" ", strip=True)
    m = MARK_RE.search(full_text)
    return (m.group(0) if m else None), None
# -------------------- PEGCLOUD --------------------
def scrape_pegcloud(url):
    """
    Επιστρέφει (mark, counterpart_vat)
    Για URLs όπως: https://e-invoicing.pegcloud.io/pegasus/einv02/search_invoice01.php?auth_code=...
    Εξάγει το MARK και ΑΦΜ πελάτη από το HTML της σελίδας.
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)
    
    try:
        r = sess.get(url, timeout=15)
        r.raise_for_status()
        r.encoding = 'utf-8'
    except Exception as e:
        print(f"[RequestError] {e}")
        return None, None
    
    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    
    # 1) MARK - Αναζήτηση "Μ.Αρ.Κ.:" 
    mark = None
    page_text = soup.get_text(" ", strip=True)
    
    # Pattern 1: Αναζήτηση στο HTML για το "Μ.Αρ.Κ.: XXX"
    mark_match = re.search(r'(?:Μ\.Αρ\.Κ\.|MARK)\s*[:]\s*([0-9]{15})', html, re.I)
    if mark_match:
        mark = mark_match.group(1)
    
    # Pattern 2: Regex fallback για 15ψήφιο αριθμό
    if not mark:
        m = re.search(r'\b([0-9]{15})\b', html)
        if m:
            mark = m.group(1)
    
    # 2) ΑΦΜ Πελάτη - Αναζήτηση στο τμήμα "Στοιχεία Πελάτη"
    counterpart_vat = None
    
    # Pattern 1: Βρες το section "Στοιχεία Πελάτη" και μετά "ΑΦΜ"
    customer_section = soup.find(string=re.compile(r"Στοιχεία Πελάτη", re.I))
    if customer_section:
        parent = customer_section.find_parent()
        if parent:
            # Αναζήτηση στο επόμενο row που περιέχει ΑΦΜ
            for row in parent.find_all("div", class_="row"):
                row_text = row.get_text(" ", strip=True)
                if "ΑΦΜ" in row_text or "Α.Φ.Μ" in row_text:
                    # Πάρε το τελευταίο div (που περιέχει την τιμή)
                    cols = row.find_all("div")
                    if cols:
                        vat_text = cols[-1].get_text(strip=True)
                        m = re.search(r'([0-9]{9})', vat_text)
                        if m:
                            counterpart_vat = m.group(1)
                            break
    
    # Pattern 2: Fallback - βρες όλα τα 9ψήφια και πάρε το πρώτο που δεν είναι του εκδότη
    if not counterpart_vat:
        all_vats = re.findall(r'\b([0-9]{9})\b', html)
        if len(all_vats) >= 2:
            # Πάρε το δεύτερο (συνήθως πελάτης)
            counterpart_vat = all_vats[1]
        elif all_vats:
            counterpart_vat = all_vats[0]
    
    return mark, counterpart_vat


# -------------------- E-INVOICING.GR (PEPPOL) --------------------
def scrape_einvoicing_gr(url, return_meta=False):
    """
    Επιστρέφει (mark, counterpart_vat) ή (mark, counterpart_vat, meta) όταν return_meta=True
    Για URLs όπως: https://e-invoicing.gr/edocuments/ViewInvoice...
    1) Ψάχνει για κουμπί "Παραστατικό (ΑΑΔΕ)" → πηγαίνει μέσω mydatapi
    2) Αλλιώς χρησιμοποιεί API endpoint (για PEPPOL URLs)
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)

    def _pack(mark_val, afm_val, is_receipt=False, doc_type=None):
        meta = {
            "is_receipt": bool(is_receipt),
            "doc_type": (doc_type or "")
        }
        if return_meta:
            return mark_val, afm_val, meta
        return mark_val, afm_val

    afm_hint = None
    doc_type_hint = ""
    receipt_hint = False

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

    def _try_mydatapi_extract(myd_url):
        if not myd_url:
            return None, None, False, ""
        try:
            rr = sess.get(myd_url, timeout=15, allow_redirects=True)
            rr.raise_for_status()
            rr.encoding = rr.apparent_encoding or 'utf-8'
            resolved = _extract_mydatapi_url_from_text(rr.url, rr.url) or _extract_mydatapi_url_from_text(rr.text, rr.url) or rr.url
            data = scrape_mydatapi(resolved, debug=False)
        except Exception:
            return None, None, False, ""

        if not data:
            return None, None, False, ""
        mark = (data.get("MARK") or "").strip()
        afm = (data.get("ΑΦΜ Πελάτη") or "").strip()
        afm = re.sub(r"\D", "", afm) if afm else None
        doc_type = str(data.get("Είδος Παραστατικού") or "").strip()
        is_receipt = _looks_like_retail_receipt(doc_type)
        mark_str = mark if mark and mark != "N/A" else None
        if afm == "N/A":
            afm = None
        if is_receipt:
            afm = None
        return mark_str, afm, is_receipt, doc_type

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

    def _looks_like_retail_receipt(text):
        if not text:
            return False
        return bool(re.search(r"απόδειξ|αποδειξ|λιανικ|receipt", text, re.I))
    
    # Πρώτα, φόρτωσε τη σελίδα για να ψάξεις myDATA URL / κουμπί
    try:
        r_initial = sess.get(url, timeout=15)
        r_initial.raise_for_status()
    except Exception as e:
        print(f"[RequestError] {e}")
        return None, None
    
    html_initial = _response_text(r_initial)
    soup_initial = BeautifulSoup(html_initial, "html.parser")

    # 0) Ψάξε πρώτα για κουμπί "Παραστατικό (ΑΑΔΕ)" που οδηγεί σε mydatapi
    # Αυτό είναι το προτιμητέο, γιατί δίνει πρώσβαση στο mydatapi flow
    # Ψάξε για <a> που περιέχει text "Παραστατικό" και έχει href
    mydatapi_url_button = None
    for a_tag in soup_initial.find_all("a", href=True):
        a_text = a_tag.get_text(strip=True)
        if "Παραστατικό" in a_text and ("ΑΑΔΕ" in a_text or "mydata" in a_tag.get("href", "").lower()):
            mydatapi_url_button = urljoin(r_initial.url, a_tag.get("href"))
            break
    
    if mydatapi_url_button:
        try:
            mark_str, afm, is_receipt, doc_type = _try_mydatapi_extract(mydatapi_url_button)
            if mark_str:
                return _pack(mark_str, afm, is_receipt=is_receipt, doc_type=doc_type)
            if afm and not afm_hint:
                afm_hint = afm
            if doc_type and not doc_type_hint:
                doc_type_hint = doc_type
            receipt_hint = receipt_hint or bool(is_receipt)
        except Exception:
            pass

    # 1) Άμεση εξαγωγή embedded mydatapi URL από HTML/scripts
    embedded_myd = _extract_mydatapi_url_from_text(html_initial, r_initial.url)
    if embedded_myd:
        try:
            mark_str, afm, is_receipt, doc_type = _try_mydatapi_extract(embedded_myd)
            if mark_str:
                return _pack(mark_str, afm, is_receipt=is_receipt, doc_type=doc_type)
            if afm and not afm_hint:
                afm_hint = afm
            if doc_type and not doc_type_hint:
                doc_type_hint = doc_type
            receipt_hint = receipt_hint or bool(is_receipt)
        except Exception:
            pass

    # 2) Headless fallback ΜΟΝΟ ως εναλλακτική όταν δεν βρέθηκε καθόλου
    # κουμπί/URL για «Παραστατικό (ΑΑΔΕ)» στο static HTML.
    if (not mydatapi_url_button) and (not embedded_myd):
        browser_myd = _resolve_mydatapi_via_browser(url, timeout=20, debug=False)
        if browser_myd:
            try:
                mark_str, afm, is_receipt, doc_type = _try_mydatapi_extract(browser_myd)
                if mark_str:
                    return _pack(mark_str, afm, is_receipt=is_receipt, doc_type=doc_type)
                if afm and not afm_hint:
                    afm_hint = afm
                if doc_type and not doc_type_hint:
                    doc_type_hint = doc_type
                receipt_hint = receipt_hint or bool(is_receipt)
            except Exception:
                pass
    
    # Fallback: χρησιμοποίησε την παλιά λογική (API endpoint ή HTML parsing)
    parsed = urlparse(url)
    
    # Αν είναι ήδη API URL, χρησιμοποίησέ το
    if "/api/GetInvoice" in parsed.path:
        api_url = url
    else:
        # Μετατροπή ViewInvoice → API endpoint
        qs = parse_qs(parsed.query)
        base = f"{parsed.scheme}://{parsed.netloc}"

        # Legacy schema: ct/id/s/h
        ct = qs.get("ct", [""])[0]
        doc_id = qs.get("id", [""])[0]
        source = qs.get("s", [""])[0]
        hash_token = qs.get("h", [""])[0]

        # New PEPPOL schema used by e-invoicing.gr: v/ag/c
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
            return None, None
    
    try:
        r = sess.get(api_url, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"[RequestError] {e}")
        return None, None
    
    html = _response_text(r)
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text("\n", strip=True)
    is_retail_receipt = _looks_like_retail_receipt(page_text)

    # 0.1) Δεύτερη ευκαιρία embedded mydatapi μέσα στο API payload
    embedded_myd_api = _extract_mydatapi_url_from_text(html, r.url)
    if embedded_myd_api:
        mark_str, afm, is_receipt, doc_type = _try_mydatapi_extract(embedded_myd_api)
        if mark_str:
            return _pack(mark_str, afm, is_receipt=is_receipt, doc_type=doc_type)
        if afm and not afm_hint:
            afm_hint = afm
        if doc_type and not doc_type_hint:
            doc_type_hint = doc_type
        receipt_hint = receipt_hint or bool(is_receipt)

    # 0.2) Αν το API payload είναι «άδειο» από labels, δοκίμασε rendered περιεχόμενο
    if not re.search(r"ΑΦΜ|Α\.Φ\.Μ|ΣΤΟΙΧΕΙΑ\s*ΠΕΛΑΤ|M\.AR\.K|MARK|ΑΝΑΛΥΣΗ\s*ΦΠΑ|ΤΙΜΟΛ|ΑΠΟΔΕΙΞ", page_text, re.I):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(api_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                rendered_html = page.content()
                browser.close()
            if rendered_html:
                soup = BeautifulSoup(rendered_html, "html.parser")
                html = rendered_html
                page_text = soup.get_text("\n", strip=True)
                is_retail_receipt = _looks_like_retail_receipt(page_text)
        except Exception:
            pass
    
    # 1) MARK - Αναζήτηση στο HTML
    mark = None
    
    # Pattern 1: label-aware extraction γύρω από το M.AR.K block
    if not mark:
        idx = html.find("M.AR.K")
        if idx < 0:
            idx = html.upper().find("MARK:")
        if idx >= 0:
            window = html[idx:idx + 500]
            mark = _clean_mark_candidate(window)

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

    # Pattern 1c: strict regex fallback γύρω από το label
    if not mark:
        m_label = re.search(r"M\.AR\.K:\s*</div>\s*<div[^>]*>\s*([^<]{1,120})\s*</div>", html, re.I | re.S)
        if m_label:
            mark = _clean_mark_candidate(m_label.group(1))
    
    # Pattern 2: Regex fallback για 15ψήφιο
    if not mark:
        m = re.search(r'\b([0-9]{15})\b', html)
        if m:
            mark = _clean_mark_candidate(m.group(1))
    
    # 2) ΑΦΜ Πελάτη - Αναζήτηση στο HTML
    counterpart_vat = None
    
    # Pattern 1: Βρες το section "ΣΤΟΙΧΕΙΑ ΠΕΛΑΤΗ" και μετά "Α.Φ.Μ:"
    customer_section = soup.find(string=re.compile(r"ΣΤΟΙΧΕΙΑ ΠΕΛΑΤΗ", re.I))
    if customer_section:
        # Βρες το parent div και ψάξε για ΑΦΜ
        parent = customer_section.find_parent("div", class_=lambda c: c and "backgrey" in c)
        if parent:
            for row in parent.find_all("div", class_="row"):
                row_text = row.get_text(" ", strip=True)
                if "Α.Φ.Μ" in row_text or "ΑΦΜ" in row_text:
                    cols = row.find_all("div", class_=lambda c: c and "col" in c)
                    if len(cols) >= 2:
                        afm_text = cols[-1].get_text(strip=True)
                        m = re.search(r'([0-9]{9})', afm_text)
                        if m:
                            counterpart_vat = m.group(1)
                            break
    
    # Pattern 2: Fallback - μόνο για τιμολόγια (όχι λιανική απόδειξη)
    if not counterpart_vat and not is_retail_receipt:
        all_vats = re.findall(r'\b([0-9]{9})\b', html)
        # Το πρώτο ΑΦΜ είναι συνήθως του εκδότη
        if len(all_vats) >= 2:
            # Πάρε το δεύτερο (πελάτη)
            counterpart_vat = all_vats[1]
        elif all_vats:
            counterpart_vat = all_vats[0]

    # Για λιανική απόδειξη το counterpart VAT πρέπει να είναι κενό
    if is_retail_receipt:
        counterpart_vat = None
    elif not counterpart_vat and afm_hint:
        counterpart_vat = afm_hint
    
    resolved_doc_type = "Απόδειξη" if is_retail_receipt else (doc_type_hint or "")
    return _pack(mark, counterpart_vat, is_receipt=(is_retail_receipt or receipt_hint), doc_type=resolved_doc_type)


# -------------------- EPSILON --------------------
def scrape_epsilon(url):
    # --- ΝΕΟ: κανονικοποίηση fd → DocViewer/UUID ---
    def _normalize_fd_to_docviewer(u: str) -> str:
        p = urlparse(u)
        # Αν είναι ήδη DocViewer, μην το πειράξεις
        if "/DocViewer/" in p.path:
            return u
        # Πιάσε το κομμάτι μετά το /fd/
        m = re.search(r"/fd/([^/?#]+)", p.path, flags=re.I)
        if not m:
            return u
        token = m.group(1)
        # Κόψε ό,τι υπάρχει μετά το ':'
        token = token.split(":")[0]
        # Κράτα μόνο hex και βεβαιώσου ότι είναι 32 ψηφία
        hexonly = re.sub(r"[^0-9a-fA-F]", "", token)
        if len(hexonly) != 32:
            return u
        # Μετατροπή 32-hex σε UUID (8-4-4-4-12)
        docid = f"{hexonly[0:8]}-{hexonly[8:12]}-{hexonly[12:16]}-{hexonly[16:20]}-{hexonly[20:32]}"
        return f"{p.scheme}://{p.netloc}/DocViewer/{docid}"

    # Εφάρμοσε κανονικοποίηση στην είσοδο
    url = _normalize_fd_to_docviewer(url)

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    # documentId
    docid = None
    if "/DocViewer/" in parsed.path:
        docid = parsed.path.split("/DocViewer/")[-1]
    else:
        q = parse_qs(parsed.query)
        if "documentId" in q:
            docid = q["documentId"][0]
    if not docid:
        return None, None, {"error": "documentId not found", "attempt_url": url}

    getfile_url = f"{base}/filedocument/getfile?fileType=3&documentId={docid}"
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        r = sess.get(getfile_url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        return None, None, {"error": f"Request failed: {e}", "attempt_url": getfile_url}

    mark = None
    counterpart_vat = None
    if "xml" in r.headers.get("Content-Type", "").lower() or r.text.strip().startswith("<"):
        try:
            ns = {"a": "http://www.aade.gr/myDATA/invoice/v1.0"}
            root = ET.fromstring(r.content)
            mark_el = root.find(".//a:mark", ns)
            if mark_el is not None:
                mark = mark_el.text.strip()
            counterpart_vat_el = root.find(".//a:counterpart/a:vatNumber", ns)
            if counterpart_vat_el is not None:
                counterpart_vat = counterpart_vat_el.text.strip()
        except Exception as e:
            return None, None, {"error": f"XML parse failed: {e}", "attempt_url": getfile_url}
    else:
        soup = BeautifulSoup(r.text, "html.parser")
        sel = soup.select_one("span.field.field-Mark span.value, span.field-Mark span.value")
        if sel:
            mark = sel.get_text(strip=True)
        txt = soup.get_text(" ", strip=True)
        if not mark:
            m = re.search(r"\b\d{15}\b", txt)
            if m:
                mark = m.group(0)
        m2 = re.search(r"(?:AFM|vatNumber|crvatnumber)[\"']?\s*[:=]?\s*[\"']?(\d{9})[\"']?", txt, re.I)
        if m2:
            counterpart_vat = m2.group(1)

    return mark, counterpart_vat, {"attempt_url": getfile_url, "status_code": r.status_code}


# -------------------- VS.GR --------------------
def scrape_vsgr(url):
    """
    Επιστρέφει (marks, counterpart_vat)
    Για URLs όπως: https://vs.gr/iv/invoice/download/.../...
    Προσθέτει ?peppol=true και εξάγει MARK και ΑΦΜ πελάτη από PEPPOL visualization.
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)

    def _looks_like_retail_receipt(text):
        if not text:
            return False
        return bool(re.search(r"απόδειξ|αποδειξ|λιανικ|receipt|αλπ|11\.1", text, re.I))

    # Προσθέσε ?peppol=true parameter και δοκίμασε και το αρχικό URL.
    peppol_url = url + ("&" if "?" in url else "?") + "peppol=true"
    html = None
    page_url = url
    last_error = None
    for candidate in (peppol_url, url):
        try:
            r = sess.get(candidate, timeout=15, allow_redirects=True)
            r.raise_for_status()
            r.encoding = 'utf-8'
            html = r.text
            page_url = r.url
            # prefer content that actually contains useful labels/myDATA hints
            if re.search(r"M\.AR\.K|MARK|Αναγνωριστικό\s*ΦΠΑ\s*Αγοραστή|myDATA|TimologioQR|Απόδειξ|Λιαν", html, re.I):
                break
        except Exception as e:
            last_error = e

    if html is None:
        if last_error:
            print(f"[RequestError] {last_error}")
        return [], None

    soup = BeautifulSoup(html, "html.parser")

    # 0) Preferred: resolve myDATA URL and delegate to mydatapi parser.
    mydatapi_url = _extract_mydatapi_url_from_text(html, page_url)
    if not mydatapi_url:
        for a in soup.find_all("a", href=True):
            href = (a.get("href") or "").strip()
            txt = (a.get_text(" ", strip=True) or "")
            blob = f"{txt} {href}"
            if re.search(r"mydata|timologioqr|qrinfo|παραστατικ", blob, re.I):
                mydatapi_url = _extract_mydatapi_url_from_text(href, page_url) or urljoin(page_url, href)
                if mydatapi_url:
                    break
    if not mydatapi_url:
        for sc in soup.find_all("script"):
            sc_text = sc.string or sc.get_text() or ""
            direct = _extract_mydatapi_url_from_text(sc_text, page_url)
            if direct:
                mydatapi_url = direct
                break

    if not mydatapi_url:
        mydatapi_url = _resolve_mydatapi_via_browser(page_url, timeout=20, debug=False)

    if mydatapi_url:
        data = scrape_mydatapi(mydatapi_url, debug=False)
        mark = (data.get("MARK") or "").strip() if data else ""
        doc_type = (data.get("Είδος Παραστατικού") or "").strip() if data else ""
        afm = (data.get("ΑΦΜ Πελάτη") or "").strip() if data else ""
        afm = re.sub(r"\D", "", afm) if afm and afm != "N/A" else None
        if _looks_like_retail_receipt(doc_type):
            afm = None
        marks = [mark] if mark and mark != "N/A" else []
        if marks or afm is not None:
            return marks, afm
    
    # 1) MARK - Αναζήτηση του 15ψήφιου νούμερου
    mark = None
    m = re.search(r'\b(\d{15})\b', html)
    if m:
        mark = m.group(1)
    
    # 2) ΑΦΜ Πελάτη - Ψάξε τις σειρές με structured approach
    counterpart_vat = None
    
    # Pattern 1: Ψάξε ΑΚΡΙΒΩΣ για "Αναγνωριστικό ΦΠΑ Αγοραστή (ΒΤ-48)" label
    # Αυτή η σειρά περιέχει το σωστό VAT του πελάτη
    for row in soup.find_all("tr"):
        row_text = row.get_text(" ", strip=True)
        # Ψάξε για το ακριβές pattern
        if re.search(r"Αναγνωριστικό ΦΠΑ Αγοραστή.*ΒΤ-48", row_text, re.I):
            # Βρες το span/td που περιέχει το VAT (συνήθως το τελευταίο)
            spans = row.find_all("span")
            if spans:
                for span in reversed(spans):  # αναζήτησε από τέλος προς αρχή
                    text = span.get_text(strip=True)
                    m_vat = re.search(r'(\d{9})', text)
                    if m_vat:
                        counterpart_vat = m_vat.group(1)
                        break
            if counterpart_vat:
                break
    
    # Pattern 2: Fallback - ψάξε όλα τα table rows με "Αναγνωριστικό" label
    if not counterpart_vat:
        for row in soup.find_all("tr"):
            row_text = row.get_text(" ", strip=True)
            if re.search(r"Αναγνωριστικό ΦΠΑ Αγοραστή", row_text, re.I) or re.search(r"BT-48", row_text, re.I):
                spans = row.find_all("span")
                if spans:
                    for span in reversed(spans):
                        text = span.get_text(strip=True)
                        m_vat = re.search(r'(\d{9})', text)
                        if m_vat:
                            counterpart_vat = m_vat.group(1)
                            break
                if counterpart_vat:
                    break
    
    # Pattern 3: Last resort - αναζήτησε ΟΛΑ τα 9ψήφια και πάρε το δεύτερο (αγοραστή)
    # ΑΛΛΑ μόνο αν υπάρχουν τουλάχιστον 2
    if not counterpart_vat:
        all_vats = re.findall(r'\b(\d{9})\b', html)
        if len(all_vats) >= 2:
            # Το πρώτο είναι πωλητής, το δεύτερο πελάτης
            counterpart_vat = all_vats[1]

    # For retail receipts we should not keep counterpart VAT.
    if _looks_like_retail_receipt(soup.get_text(" ", strip=True)):
        counterpart_vat = None
    
    marks = [mark] if mark else []
    return marks, counterpart_vat


# -------------------- PRIMER --------------------
def _extract_primer_mark_from_url(url):
    parsed = urlparse(url)
    segment = parsed.path.rstrip("/").split("/")[-1]
    if re.fullmatch(r"\d{15}", segment):
        return segment
    m = re.search(r"/(\d{15})(?:[/?#]|$)", url)
    return m.group(1) if m else None


def scrape_primer(url):
    """
    Επιστρέφει (marks, counterpart_vat) για URL τύπου mydata.primer.gr.
    Το MARK μπορεί να είναι ήδη ενσωματωμένο στο URL, ενώ το ΑΦΜ
    πελάτη αναζητείται στη σελίδα ή στο embedded mydatapi flow.
    """
    mark = _extract_primer_mark_from_url(url)
    sess = requests.Session()
    sess.headers.update(HEADERS)

    try:
        r = sess.get(url, timeout=15)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"[RequestError] {e}")
        return [mark] if mark else [], None

    mydatapi_url = _extract_mydatapi_url_from_text(html, r.url)
    if mydatapi_url:
        try:
            data = scrape_mydatapi(mydatapi_url, debug=False)
            if data:
                extracted_mark = data.get("MARK")
                if extracted_mark and extracted_mark != "N/A":
                    mark = mark or extracted_mark.strip()
                afm = data.get("ΑΦΜ Πελάτη") or data.get("ΑΦΜ")
                if afm and afm != "N/A":
                    return ([mark] if mark else []), re.sub(r"\D", "", afm)
        except Exception:
            pass

    xml_fragment = _extract_xml_fragment(html)
    if xml_fragment:
        try:
            root = ET.fromstring(xml_fragment.encode('utf-8'))
            extracted_mark = _extract_mark_from_primer_xml(root)
            if extracted_mark:
                mark = mark or extracted_mark
            seller_vat = None
            issuer = root.find('.//issuer') or root.find('.//{*}issuer')
            if issuer is not None:
                seller_el = issuer.find('.//vatNumber') or issuer.find('.//{*}vatNumber') or issuer.find('.//vatnumber')
                if seller_el is not None and seller_el.text:
                    m = VAT_RE.search(seller_el.text.strip())
                    if m:
                        seller_vat = m.group(0)
            afm = _extract_vat_from_primer_xml(root, seller_vat=seller_vat)
            if afm:
                return ([mark] if mark else []), afm
        except Exception:
            pass

    # Προτίμηση σε εμφανή ΑΦΜ με ετικέτα
    soup = BeautifulSoup(html, "html.parser")
    counterpart_vat = None
    for lbl in soup.find_all(string=re.compile(r"Α\.?Φ\.?Μ\.?", re.I)):
        parent = lbl.parent
        if parent:
            text = parent.get_text(" ", strip=True)
            m = re.search(r"Α\.?Φ\.?Μ\.?[:\s]*([0-9]{9})", text, flags=re.I)
            if m:
                counterpart_vat = m.group(1)
                break
    if not counterpart_vat:
        page_text = soup.get_text(" ", strip=True)
        m = re.search(r"\b([0-9]{9})\b", page_text)
        if m:
            counterpart_vat = m.group(1)

    if counterpart_vat:
        counterpart_vat = re.sub(r"\D", "", counterpart_vat)

    return ([mark] if mark else []), counterpart_vat


def _extract_xml_fragment(text):
    if not text:
        return None
    # Pages may include raw myDATA XML in the page source when the myDATA view
    # is selected. Extract the XML block so we can parse it directly.
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


def _parse_xml_content_for_marks_and_vat(html):
    xml_fragment = _extract_xml_fragment(html)
    if not xml_fragment:
        return None
    try:
        root = ET.fromstring(xml_fragment.encode('utf-8'))
        marks, vat = _extract_from_root(root)
        if marks or vat:
            return marks, vat
    except Exception:
        pass
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


# -------------------- MEGASOFT --------------------
def scrape_megasoft(url):
    """
    Προσπαθεί να εντοπίσει και να ακολουθήσει το "Προβολή μέσω myDATA" link.
    Αν βρει mydatapi URL, χρησιμοποιεί scrape_mydatapi.
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)

    candidates = [url + ("&" if "?" in url else "?") + "peppol=true", url]
    html = None
    base = url

    for cu in candidates:
        try:
            r = sess.get(cu, timeout=20, allow_redirects=True)
            r.raise_for_status()
            html = r.text
            base = r.url
            if re.search(r"Αναγνωριστικό\s*ΦΠΑ\s*Αγοραστή|ΒΤ-48|myDATA|TimologioQR", html, re.I):
                break
        except Exception:
            continue

    if not html:
        return [], None

    xml_result = _parse_xml_content_for_marks_and_vat(html)
    if xml_result:
        return xml_result

    soup = BeautifulSoup(html, "html.parser")
    mydatapi_url = _extract_mydatapi_url_from_text(html, base)
    if not mydatapi_url:
        parsed = urlparse(url)
        qrcode_value = parse_qs(parsed.query).get("QrCode", [""])[0]
        candidate_targets = []
        button_ids = []

        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            txt = (a.get_text(" ", strip=True) or "") + " " + href
            if "mydata" in txt.lower() or "timologioqr" in txt.lower() or "invoiceinspect/mydata" in txt.lower():
                candidate_targets.append(urljoin(base, href))

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
                candidate_targets.append(urljoin(base, formaction))

            form_id = btn.get("form")
            if form_id:
                form_el = soup.find("form", attrs={"id": form_id})
                if form_el and form_el.get("action"):
                    candidate_targets.append(urljoin(base, form_el.get("action")))

            parent_form = btn.find_parent("form")
            if parent_form and parent_form.get("action"):
                candidate_targets.append(urljoin(base, parent_form.get("action")))

            onclick = str(btn.get("onclick", ""))
            m_on = re.search(r"(?:location\.href\s*=|window\.open\s*\(|window\.location(?:\.href)?\s*=)\s*['\"]([^'\"]+)['\"]", onclick, re.I)
            if m_on:
                candidate_targets.append(urljoin(base, m_on.group(1)))

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
                    candidate_targets.append(urljoin(base, str(val)))

            onclick = str(attrs.get("onclick", ""))
            m = re.search(r"(?:location\.href|window\.open|window\.location(?:\.href)?)\s*\(\s*['\"]([^'\"]+)['\"]", onclick, re.I)
            if m:
                candidate_targets.append(urljoin(base, m.group(1)))

        # Search script handlers by button id / generic mydata urls
        for script in soup.find_all("script"):
            sc = script.string or script.get_text() or ""
            direct = _extract_mydatapi_url_from_text(sc, base)
            if direct:
                candidate_targets.append(direct)

            for m in re.finditer(r"/(?:invoiceinspect/mydata|invoiceinspect/qr)[^\s\"\'<>]*", sc, re.I):
                candidate_targets.append(urljoin(base, m.group(0)))

            for bid in button_ids:
                if bid and bid in sc:
                    m2 = re.search(r"(?:location\.href\s*=|window\.open\s*\(|window\.location(?:\.href)?\s*=)\s*['\"]([^'\"]+)['\"]", sc, re.I)
                    if m2:
                        candidate_targets.append(urljoin(base, m2.group(1)))

        if qrcode_value:
            q_enc = requests.utils.quote(qrcode_value, safe="")
            candidate_targets.extend([
                urljoin(base, f"/invoiceinspect/mydata?QrCode={q_enc}"),
                urljoin(base, f"/invoiceinspect/qr?QrCode={q_enc}&openMydata=true"),
                urljoin(base, f"/invoiceinspect/qr?QrCode={q_enc}&mydata=true"),
            ])

        seen = set()
        candidate_targets = [c for c in candidate_targets if c and not (c in seen or seen.add(c))]

        for target in candidate_targets:
            try:
                rr = sess.get(target, timeout=20, allow_redirects=True)
                rr.raise_for_status()
                xml_result = _parse_xml_content_for_marks_and_vat(rr.text)
                if xml_result:
                    return xml_result
                mydatapi_url = _extract_mydatapi_url_from_text(rr.url, rr.url) or _extract_mydatapi_url_from_text(rr.text, rr.url)
                if mydatapi_url:
                    break
            except Exception:
                continue

    if mydatapi_url:
        data = scrape_mydatapi(mydatapi_url)
        mark = data.get("MARK") if data else None
        afm = data.get("ΑΦΜ Πελάτη") if data else None
        marks = [mark] if mark and mark != "N/A" else []
        counterpart_vat = afm if afm and afm != "N/A" else None
        return marks, counterpart_vat

    # JS-aware fallback: real button click via headless browser
    browser_url = _resolve_mydatapi_via_browser(url, timeout=20, debug=False)
    if browser_url:
        data = scrape_mydatapi(browser_url)
        mark = data.get("MARK") if data else None
        afm = data.get("ΑΦΜ Πελάτη") if data else None
        marks = [mark] if mark and mark != "N/A" else []
        counterpart_vat = afm if afm and afm != "N/A" else None
        return marks, counterpart_vat

    # Do not return AFM from blind QrCode decode fallback: often misleading.
    return [], None


def scrape_iview(url):
    """
    Επιστρέφει (marks, counterpart_vat) για URLs τύπου iview.gr.
    Ακολουθεί τον σύνδεσμο mydatapi που βρίσκεται στο button/anchor και
    συνεχίζει με το scrape_mydatapi για να διαβάσει MARK και ΑΦΜ.
    """
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        r = sess.get(url, timeout=15)
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"[RequestError] {e}")
        return [], None

    mydatapi_url = _extract_mydatapi_url_from_text(html, r.url)
    if not mydatapi_url:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "mydatapi.aade.gr" in href and "TimologioQR/QRInfo" in href:
                mydatapi_url = urljoin(r.url, href)
                break
        if not mydatapi_url:
            for btn in soup.find_all("button"):
                link = btn.find("a", href=True)
                if link:
                    href = link["href"].strip()
                    if "mydatapi.aade.gr" in href and "TimologioQR/QRInfo" in href:
                        mydatapi_url = urljoin(r.url, href)
                        break

    if mydatapi_url:
        data = scrape_mydatapi(mydatapi_url, debug=False)
        mark = (data.get("MARK") or "").strip()
        afm = (data.get("ΑΦΜ Πελάτη") or "").strip()
        if afm == "N/A":
            afm = None
        marks = [mark] if mark and mark != "N/A" else []
        return marks, afm

    return [], None


# -------------------- MAIN --------------------
def main():
    url = input("Εισάγετε το URL: ").strip()
    url = _normalize_url(url)
    domain = urlparse(url).netloc.lower()
    data = {}
    marks = []
    counterpart_vat = None
    source = None

    if "wedoconnect" in domain:
        source = "Wedoconnect"
        marks, counterpart_vat = scrape_wedoconnect(url)

    elif "mydatapi.aade.gr" in domain:
        source = "MyData"
        data = scrape_mydatapi(url)
        marks = [data.get("MARK", "N/A")]

    elif "einvoice.s1ecos.gr" in domain:
        source = "ECOS E-Invoicing"
        marks, counterpart_vat = scrape_einvoice(url)

    elif "einvoice.impact.gr" in domain or "impact.gr" in domain:
        source = "Impact E-Invoicing"
        mark, counterpart_vat = scrape_impact(url)
        marks = [mark] if mark else []

    elif "epsilonnet.gr" in domain:
        source = "Epsilon (myData)"
        mark, counterpart_vat, info = scrape_epsilon(url)
        marks = [mark] if mark else []

    elif "parochos.gr" in domain:
        source = "Parochos (myData)"
        mark, counterpart_vat, info = scrape_epsilon(url)
        marks = [mark] if mark else []

    elif "e-invoicing.pegcloud.io" in domain:
        source = "Pegcloud e-Invoicing"
        mark, counterpart_vat = scrape_pegcloud(url)
        marks = [mark] if mark else []

    elif "e-invoicing.gr" in domain:
        source = "e-Invoicing.gr (PEPPOL)"
        # In standalone CLI usage, prefer browser fallback for dynamic e-invoicing pages
        # when the MyData button/link is not present in static HTML.
        os.environ.setdefault("MYDATA_USE_BROWSER", "1")
        mark, counterpart_vat = scrape_einvoicing_gr(url)
        marks = [mark] if mark else []

    elif "vs.gr" in domain:
        source = "VS.gr"
        marks, counterpart_vat = scrape_vsgr(url)

    elif "mydata.primer.gr" in domain or "primer.gr" in domain:
        source = "Primer MyData"
        marks, counterpart_vat = scrape_primer(url)

    elif "iview.gr" in domain:
        source = "IView"
        marks, counterpart_vat = scrape_iview(url)

    elif "megasoft" in domain or "invoicelink" in domain:
        source = "Megasoft"
        marks, counterpart_vat = scrape_megasoft(url)

    else:
        print("Άγνωστο URL. Δεν μπορεί να γίνει scrape.")
        return

    print(f"\nΠηγή: {source}")
    if marks:
        print("\nΒρέθηκαν MARK(s):")
        for m in marks:
            print(m)
    else:
        print("Δεν βρέθηκε MARK.")

    # Εκτύπωση ΑΦΜ / counterpart VAT για κάθε πηγή
    if source == "Wedoconnect":
        if counterpart_vat:
            print("counterpart VAT:", counterpart_vat)
        else:
            print("Δεν βρέθηκε ΑΦΜ πελάτη.")

    if source == "MyData":
        afm = data.get("ΑΦΜ Πελάτη", "")
        if afm:
            print("ΑΦΜ Πελάτη:", afm)
        if "απόδειξη" in data.get("Είδος Παραστατικού", "").lower():
            print("⚠️ Πρόκειται για απόδειξη!")

    if source == "ECOS E-Invoicing":
        if counterpart_vat:
            print("counterpart VAT:", counterpart_vat)

    if source == "Impact E-Invoicing":
        if counterpart_vat:
            print("ΑΦΜ Πελάτη:", counterpart_vat)
        else:
            print("Δεν βρέθηκε ΑΦΜ πελάτη.")

    if source == "Epsilon (myData)":
        if counterpart_vat:
            print("counterpart VAT:", counterpart_vat)

    if source == "Parochos (myData)":
        if counterpart_vat:
            print("counterpart VAT:", counterpart_vat)

    if source == "Pegcloud e-Invoicing":
        if counterpart_vat:
            print("ΑΦΜ Πελάτη:", counterpart_vat)
        else:
            print("Δεν βρέθηκε ΑΦΜ πελάτη.")

    if source == "e-Invoicing.gr (PEPPOL)":
        if counterpart_vat:
            print("ΑΦΜ Πελάτη:", counterpart_vat)
        else:
            print("Δεν βρέθηκε ΑΦΜ πελάτη (πιθανή απόδειξη λιανικής).")

    if source == "VS.gr":
        if counterpart_vat:
            print("ΑΦΜ Πελάτη:", counterpart_vat)
        else:
            print("Δεν βρέθηκε ΑΦΜ πελάτη.")


if __name__ == "__main__":
    main()
