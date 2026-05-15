/* receipts_autosubmit_strict.js — Receipts-only, URL-only, deterministic auto-submit */
(function(){
  if (window.__RC_USE_CANONICAL_RECEIPT_AUTOCONFIRM === true) return;
  if (window.__rc_receipts_strict__) return;
  window.__rc_receipts_strict__ = true;

  function lsGet(k,d){ try{ const v=localStorage.getItem(k); return v==null?d:v; }catch(_){ return d; } }
  function ssGet(k){ try{ return sessionStorage.getItem(k); }catch(_){ return null; } }
  function ssSet(k,v){ try{ sessionStorage.setItem(k,v); }catch(_){ } }
  const AUTO_KEY = 'rc:autoSubmitEnabled';
  const LAST_KEY = 'rc:lastSubmitKey';
  const TTL_MS   = 20000;

  function $id(id){ return document.getElementById(id); }
  function normUrl(u){ return String(u||'').trim().replace(/\/+$/,''); }
  function isUrl(v){ const s=String(v||'').trim(); return /^https?:\/\/\S+/i.test(s) || /^www\.\S+/i.test(s); }
  function isReceipts(){ try{ const sw=$id('useReceiptsSwitch'); if (sw) return !!sw.checked; }catch(_){}
                         return lsGet('UI:useReceipts','0')==='1'; }
  function isAuto(){ try{ return localStorage.getItem(AUTO_KEY)==='1'; }catch(_){ return false; } }

  function ensureUrlInput(){
    var el = $id('scrapeUrlInput');
    var mark = $id('markInput');
    if (!el){
      el = document.createElement('input');
      el.type = 'text';
      el.id = 'scrapeUrlInput';
      el.name = 'scrape_url_client';
      el.placeholder = 'Επικόλληση URL αποδείξεων';
      el.className = mark ? mark.className : 'p-2 border rounded w-2/4';
      el.autocomplete = 'off';
      if (mark && mark.parentElement){
        mark.parentElement.insertBefore(el, mark.nextSibling);
      } else {
        document.body.appendChild(el);
      }
    }
    return el;
  }

  function readLastKey(){
    try {
      const raw = sessionStorage.getItem(LAST_KEY);
      if (!raw) return null;
      const o = JSON.parse(raw);
      if (!o || !o.ts || (Date.now()-o.ts)>TTL_MS) return null;
      return o.k || null;
    }catch(_){ return null; }
  }
  function lockSubmitForKey(k){
    try { sessionStorage.setItem(LAST_KEY, JSON.stringify({k,ts:Date.now()})); } catch(_){}
  }

  function submitFormWithUrl(u){
    const form = $id('markSearchForm');
    const mark = $id('markInput');
    if (!form || !mark) return false;
    mark.value = u;
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    return true;
  }

  function tryAuto(){
    if (!isReceipts() || !isAuto()) return;
    var urlEl = ensureUrlInput();
    var uRaw = urlEl && urlEl.value || '';
    if (!isUrl(uRaw)) return;
    var key = 'url:'+normUrl(uRaw);
    if (readLastKey() === key) return;
    if (window.__RC_LOCKS && window.__RC_LOCKS.scrape) return;
    lockSubmitForKey(key);
    submitFormWithUrl(normUrl(uRaw));
  }

  function applyUiReceiptsOnly(){
    const mark = $id('markInput');
    const url  = ensureUrlInput();
    if (isReceipts()){
      if (mark){ mark.classList.add('hidden'); mark.disabled = true; }
      if (url){ url.classList.remove('hidden'); url.disabled = false; url.focus(); }
    } else {
      if (url){ url.classList.add('hidden'); url.disabled = true; }
      if (mark){ mark.classList.remove('hidden'); mark.disabled = false; }
    }
  }

  function wireWarningsReload(){
    function reloadSoon(){
      if (typeof window.redirectToReceiptsFlow === 'function') {
        window.redirectToReceiptsFlow();
        return;
      }
      try {
        if (typeof window.clearReclassificationSearchState === 'function') window.clearReclassificationSearchState();
      } catch(_) {}
      try {
        const u = new URL(window.location.href);
        ['mark','scrape_url','force_edit'].forEach(k => u.searchParams.delete(k));
        history.replaceState(null, '', u.pathname + (u.search ? ('?' + u.searchParams.toString()) : '') + u.hash);
      } catch(_) {}
    }
    const afmBtn = $id('afmModalConfirm');
    if (afmBtn) afmBtn.addEventListener('click', reloadSoon);
    ['receiptWarningModal','afmWarningModal','yearWarningModal'].forEach(function(mid){
      const m = $id(mid);
      if (!m) return;
      m.addEventListener('click', function(ev){
        const t = ev.target;
        if (t && (t.tagName === 'BUTTON' || (t.closest && t.closest('button')))) setTimeout(reloadSoon, 10);
      });
    });
  }

  function wire(){
    if (lsGet('UI:useReceipts','0')==='1'){
      try{ const sw=$id('useReceiptsSwitch'); if (sw && !sw.checked){ sw.checked = true; sw.dispatchEvent(new Event('change',{bubbles:true})); } }catch(_){}
    }
    applyUiReceiptsOnly();
    const urlEl = ensureUrlInput();
    if (urlEl){ ['input','keyup','paste','change'].forEach(ev=> urlEl.addEventListener(ev, tryAuto)); }
    const sw = $id('useReceiptsSwitch'); if (sw) sw.addEventListener('change', ()=>{ setTimeout(()=>{ applyUiReceiptsOnly(); tryAuto(); }, 0); });
    const rep = $id('repeatEntrySwitch'); if (rep) rep.addEventListener('change', ()=> setTimeout(tryAuto,10));
    window.addEventListener('load', ()=> setTimeout(tryAuto, 60));
    wireWarningsReload();
  }

  document.addEventListener('DOMContentLoaded', wire);
})();
