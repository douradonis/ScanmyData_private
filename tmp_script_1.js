
// Loading overlay helpers
function showLoadingOverlay(title, message){
  try{
    const overlay = document.getElementById('loadingOverlay');
    const t = document.getElementById('loadingOverlayTitle');
    const m = document.getElementById('loadingOverlayMessage');
    if(t) t.textContent = title || 'Παρακαλώ περιμένετε...';
    if(m) m.textContent = message || 'Επεξεργασία δεδομένων';
    if(overlay){ overlay.style.display = 'flex'; overlay.classList.remove('hidden'); }
  }catch(e){ /* ignore */ }
}

function hideLoadingOverlay(){
  try{
    const overlay = document.getElementById('loadingOverlay');
    if(overlay){ overlay.style.display = 'none'; overlay.classList.add('hidden'); }
  }catch(e){}
}

// Convenience wrapper for long fetches that should show the overlay
async function fetchWithOverlay(url, opts, title, message){
  showLoadingOverlay(title || 'Παρακαλώ περιμένετε...', message || 'Επικοινωνία με τον διακομιστή...');
  try{
    const res = await fetch(url, opts);
    hideLoadingOverlay();
    return res;
  }catch(err){
    hideLoadingOverlay();
    throw err;
  }
}

// Enhance flash banners: add close button and auto-dismiss with fade
function enhanceFlashBanners(root=document){
  try{
    (root.querySelectorAll || Array.prototype) && root.querySelectorAll('.flash-banner').forEach(el => {
      if (el.dataset.enhanced) return; el.dataset.enhanced = '1';
      // add close button
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ml-3 close-btn text-sm text-gray-600';
      btn.innerText = '✕';
      btn.style.background = 'transparent';
      btn.style.border = 'none';
      btn.style.cursor = 'pointer';
      btn.addEventListener('click', () => { el.style.transition = 'opacity 0.35s'; el.style.opacity = '0'; setTimeout(()=>el.remove(), 360); });
      el.appendChild(btn);
      // auto-dismiss unless data-ttl="0"
      const ttl = parseInt(el.getAttribute('data-ttl') || '5000', 10);
      if (ttl > 0) {
        setTimeout(()=>{ try{ el.style.transition='opacity 0.5s'; el.style.opacity='0'; setTimeout(()=>el.remove(),520); }catch(_){}} , ttl);
      }
    });
  }catch(e){console.warn('enhanceFlashBanners failed', e);} 
}

document.addEventListener('DOMContentLoaded', function(){ enhanceFlashBanners(document); }, { once: true });

function normalizeScanValue(raw){
  try {
    const text = (raw == null) ? '' : String(raw).trim();
    if (!text) return '';
    if (!/^https?:\/\//i.test(text)) return text;
    if (typeof URL === 'undefined') return text;

    const url = new URL(text);
    const host = (url.hostname || '').toLowerCase();
    if (host.includes('epsilondigital')){
      url.pathname = (url.pathname || '/').replace(/:\d+(?=$|\/)/g, '');
      url.pathname = url.pathname.replace(/\/{2,}/g, '/');
      if (!url.pathname.startsWith('/')) url.pathname = '/' + url.pathname;
      url.pathname = url.pathname.replace(/\/+$/, '') || '/';
      url.search = '';
      url.hash = '';
    }
    return url.toString();
  } catch (err){
    return (raw == null) ? '' : String(raw).trim();
  }
}

function isReceiptsOn(){
  const r = document.getElementById('useReceiptsSwitch');
  return !!(r && r.checked);
}

function rcRepeatModalShouldUseReceiptsMode(){
  try {
    if (window.__RC_REPEAT_MODAL_FORCE_RECEIPTS) return true;
  } catch(_) {}
  try {
    if (isReceiptsOn()) return true;
  } catch(_) {}
  try {
    if (isReceiptAnalysisOn()) return true;
  } catch(_) {}
  try {
    const saved = String(localStorage.getItem('UI:useReceipts') || '').trim();
    if (saved === '1' || saved.toLowerCase() === 'true') return true;
  } catch(_) {}
  try {
    const legacySaved = String(localStorage.getItem('rc:useReceipts') || '').trim();
    if (legacySaved === '1' || legacySaved.toLowerCase() === 'true') return true;
  } catch(_) {}
  return false;
}

function isReceiptAnalysisOn(){
  // analysis state is driven only by the receipts mode chooser.
  // Do not infer from summary payload flags because that can force the
  // summary modal into analysis UI while user is in mixed mode.
  try {
    // If the receipts switch exists and is explicitly OFF, force analysis off to protect invoice flow.
    const r = document.getElementById('useReceiptsSwitch');
    if (r && !r.checked) return false;
    const mode = localStorage.getItem('rc:receiptMode');
    if (String(mode || '').toLowerCase() === 'analysis') return true;
  } catch(_){ }
  return false;
}

function rcRepeatReceiptsFlowActive(){
  try {
    const repeatOn = !!document.getElementById('repeatEntrySwitch')?.checked;
    if (!repeatOn) return false;
    return !!isReceiptsOn();
  } catch(_) { return false; }
}

function rcShouldApplyReceiptProfile(summary){
  try { if (isReceiptAnalysisOn()) return true; } catch(_) {}
  try { if (rcRepeatReceiptsFlowActive()) return true; } catch(_) {}
  try {
    if (summary && (summary.receipt_analysis_enabled === true || summary.receipts_analysis_enabled === true || summary.receiptAnalysisEnabled === true)) return true;
  } catch(_) {}
  try {
    if (!summary) {
      const raw = document.getElementById('summaryJsonInput')?.value || '';
      if (raw && raw !== '{}' && raw !== 'null') summary = JSON.parse(raw);
    }
  } catch(_) {}
  try {
    if (typeof __rcIsReceiptAnalysisContext === 'function' && __rcIsReceiptAnalysisContext(summary)) return true;
  } catch(_) {}
  return false;
}

// Broader check: treat as analysis when either chooser is set to analysis OR the
// current summary payload clearly indicates receipt-analysis flow (e.g. when the
// receipts toggle/chooser is absent in this partial render).
function isReceiptAnalysisActive(){
  try {
    if (isReceiptAnalysisOn()) return true;
  } catch(_) { /* fall through */ }
  // If chooser exists on page, treat it as authoritative.
  // This prevents mixed mode from being misread as analysis just because
  // a summary object is a receipt.
  try {
    const chooser = document.getElementById('useReceiptsSwitch');
    if (chooser) return false;
  } catch(_) { /* ignore */ }
  try {
    const summaryEl = document.getElementById('summaryJsonInput');
    if (summaryEl && summaryEl.value) {
      const obj = JSON.parse(summaryEl.value || '{}') || {};
      if (obj.receipt_analysis_enabled === true) return true;
      if (obj.receipts_analysis_enabled === true) return true;
      if (obj.receiptAnalysisEnabled === true) return true;
    }
  } catch(_) { /* ignore */ }
  return false;
}

function isReceiptModeAnalysisStrict(){
  try {
    const mode = localStorage.getItem('rc:receiptMode');
    return String(mode || '').toLowerCase() === 'analysis';
  } catch(_){
    return false;
  }
}

function rcMarkSummaryEditingWindow(ms){
  try {
    const ttl = Number.isFinite(Number(ms)) ? Number(ms) : 4000;
    window.__RC_SUPPRESS_MODAL_SWITCH_UNTIL = Date.now() + Math.max(800, ttl);
  } catch(_) {}
}

function rcShouldSuppressModalSwitch(){
  try {
    const until = Number(window.__RC_SUPPRESS_MODAL_SWITCH_UNTIL || 0);
    return until > Date.now();
  } catch(_) { return false; }
}

function receiptModeAllowsProfileHint(){
  try {
    const chooserExists = !!document.querySelector('[data-receipt-mode]');
    const stored = (localStorage.getItem('rc:receiptMode') || '').trim().toLowerCase();
    if (chooserExists) {
      return stored === 'analysis';
    }
    if (stored === 'analysis') return true;
  } catch(_) {}
  try {
    if (typeof isReceiptAnalysisOn === 'function' && isReceiptAnalysisOn()) return true;
  } catch(_) {}
  return false;
}

function setActiveProfileHint(name){
  const el = document.getElementById('activeRepeatProfileHint');
  const sw = document.getElementById('repeatEntrySwitch');
  if(!el || !sw) return;
  try { window.__LAST_REPEAT_PROFILE_NAME = name || ''; } catch(_) { window.__LAST_REPEAT_PROFILE_NAME = name || ''; }

  if (sw.checked && receiptModeAllowsProfileHint()) {
    el.style.display = '';
    el.textContent = "Ενεργό προφίλ επαναληψιμης: " + (name || "Γενικό");
  } else {
    el.style.display = 'none';
    el.textContent = '';
  }
}


function currentScannerMode(){
  try {
    const modeAttr = document.body && document.body.dataset ? document.body.dataset.mode : '';
    if (modeAttr) {
      const normalized = String(modeAttr).trim().toLowerCase();
      if (normalized === 'receipts' || normalized === 'invoices') {
        return normalized;
      }
    }
  } catch (_) {}
  return isReceiptsOn() ? 'receipts' : 'invoices';
}

function readAutoSubmitState(){
  try {
    if (window.RC && window.RC.autoSubmitControls && typeof window.RC.autoSubmitControls.get === 'function'){
      return !!window.RC.autoSubmitControls.get();
    }
  } catch (_) {}
  try {
    return localStorage.getItem('rc:autoSubmitEnabled') === '1';
  } catch (_) {
    return false;
  }
}

function applyScannedPayload(payload, options){
  options = options || {};
  const rawVal = payload && payload.raw != null ? String(payload.raw).trim() : '';
  const markVal = payload && payload.mark != null ? String(payload.mark).trim() : '';
  const normalizedRaw = normalizeScanValue(rawVal);
  const effectiveRaw = normalizedRaw || rawVal;
  if (payload && typeof payload === 'object'){
    payload.raw = effectiveRaw;
  }
  const chosen = markVal || effectiveRaw;
  if (!chosen) return null;

  let isUrl;
  if (Object.prototype.hasOwnProperty.call(options, 'forceUrl')) {
    isUrl = !!options.forceUrl;
  } else {
    isUrl = /^https?:\/\//i.test(effectiveRaw);
  }

  let mode = options.mode || currentScannerMode();
  if (mode !== 'receipts' && mode !== 'invoices') {
    mode = 'invoices';
  }

  let appliedTo = null;
  if (mode === 'receipts' && isUrl) {
    const receiptsInput = document.getElementById('scrapeUrlInput');
    if (receiptsInput) {
      receiptsInput.value = effectiveRaw;
      try { receiptsInput.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) {}
      appliedTo = 'receipts';
    }
    const hiddenUrl = document.getElementById('scrapeUrlField');
    if (hiddenUrl) hiddenUrl.value = effectiveRaw;
  } else {
    const markInput = document.getElementById('markInput');
    if (markInput) {
      const nextValue = markVal ? markVal : (isUrl ? effectiveRaw : chosen);
      markInput.value = nextValue;
      try { markInput.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) {}
      appliedTo = 'mark';
    }
  }

  return {
    appliedTo,
    raw: effectiveRaw,
    mark: markVal,
    is_url: !!isUrl,
    mode
  };
}


