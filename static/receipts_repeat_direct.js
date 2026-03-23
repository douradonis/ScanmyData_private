/* receipts_repeat_direct.js — Bypass summary modal for receipts when repeat is ON */
(function(){
  if (window.__rc_direct_bypass__) return;
  window.__rc_direct_bypass__ = true;

  function $id(id){ return document.getElementById(id); }
  function lsGet(k,d){ try{const v=localStorage.getItem(k);return v==null?d:v;}catch(_){return d;} }
  function lsSet(k,v){ try{localStorage.setItem(k,v);}catch(_){ } }
  function ssGet(k){ try{return sessionStorage.getItem(k);}catch(_){return null;} }
  function ssSet(k,v){ try{sessionStorage.setItem(k,v);}catch(_){ } }
  function ssDel(k){ try{sessionStorage.removeItem(k);}catch(_){ } }
  function onceKeyFromSummary(s){
    try {
      var mark = String((s && (s.mark || s.MARK)) || '').trim();
      var aa = String((s && (s.AA || s.aa || s.number || s.progressive_aa)) || '').trim();
      var afm = String((s && (s.AFM_issuer || s.AFM || s.issuer_vat)) || '').trim();
      var date = String((s && (s.issueDate || s.issue_date || s.date)) || '').trim();
      return 'rc-direct:' + [mark, aa, afm, date].join('|');
    } catch(_) {
      return 'rc-direct:' + String((s && (s.mark || s.MARK)) || '').trim();
    }
  }
  function isReceipts(){ try{var sw=$id('useReceiptsSwitch'); if(sw) return !!sw.checked;}catch(_){ }
                         return lsGet('UI:useReceipts','0')==='1'; }
  function isRepeat(){   try{var rs=$id('repeatEntrySwitch'); if(rs) return !!rs.checked;}catch(_){ }
                         return lsGet('REPEAT:enabled','0')==='1'; }
  function receiptMode(){
    try{
      var mode = localStorage.getItem('rc:receiptMode');
      return String(mode || 'mixed').trim().toLowerCase();
    }catch(_){ return 'mixed'; }
  }
  function isMixedMode(){
    try{
      var mode = receiptMode();
      if(!mode) return true;
      if(mode === 'manual' || mode === 'off') return false;
      return true; // allow both mixed and analysis flows
    }catch(_){ return true; }
  }
  function isAnalysisMode(){
    return receiptMode() === 'analysis';
  }
  function parseSummary(){
    var el=$id('summaryJsonInput');
    if(!el || !el.value || el.value==='{}' || el.value==='null') return null;
    try{ return JSON.parse(el.value); }catch(_){ return null; }
  }
  function isReceiptSummary(s){
    if(!s||typeof s!=='object') return false;
    if(s.is_receipt===true) return true;
    var t=String(s.type_name||s.type||'').toLowerCase();
    if(t.includes('απόδει') || t.includes('receipt')) return true;
    var ui=String(s.ui_hint||'').toLowerCase();
    if(ui.includes('λήφθηκε')&&ui.includes('αποδεί')) return true;
    return false;
  }
  function hasLines(s){
    try{
      if(Array.isArray(s.lines) && s.lines.length>0){
        for(var i=0;i<s.lines.length;i++){
          var l = s.lines[i] || {};
          var amt = String(l.amount || l.total || l.lineTotal || '').trim();
          var vat = String(l.vat || l.vatRate || '').trim();
          var cat = String(l.category || '').trim();
          if(amt || vat || cat) return true;
        }
      }
      // mixed receipts can be header-only
      var total = String((s.totalValue || s.total_amount || s.totalNetValue || '')).trim();
      var aa = String((s.AA || s.aa || s.number || s.progressive_aa || '')).trim();
      return !!(total || aa);
    }catch(_){ return false; }
  }
  function normalizeReceipt(s){
    try{
      var analysis = isReceiptAnalysisContext(s);
      s.is_receipt = true;
      if (analysis) {
        s.receipt_analysis_enabled = true;
      } else {
        s.category = s.category || 'αποδειξακια';
        s.characteristic = s.characteristic || 'αποδειξακια';
      }
      if(!Array.isArray(s.lines)) s.lines=[];
      if(!s.lines.length){
        var amountGuess = String(s.totalNetValue || s.totalValue || s.total_amount || '').trim();
        var vatGuess = String(s.totalVatAmount || s.total_vat || '').trim();
        s.lines = [{
          id: 'r0',
          description: '',
          amount: amountGuess,
          vat: vatGuess,
          vat_category: '',
          category: analysis ? '' : 'αποδειξακια'
        }];
      } else {
        s.lines = s.lines.map(function(l){
          l = l || {};
          if (analysis) {
            l.category = l.category || '';
          } else {
            l.category = l.category || 'αποδειξακια';
          }
          return l;
        });
      }
    }catch(_){ }
    return s;
  }

  function isReceiptAnalysisContext(summary){
    try {
      if (typeof isReceiptModeAnalysisStrict === 'function' && isReceiptModeAnalysisStrict()) return true;
    } catch(_) {}
    try {
      if (typeof isReceipts === 'function' && typeof isRepeat === 'function' && isReceipts() && isRepeat()) return true;
    } catch(_) {}
    try {
      var mode = receiptMode();
      if (mode === 'analysis') return true;
    } catch(_) {}
    try {
      if (typeof isReceiptAnalysisOn === 'function' && isReceiptAnalysisOn()) return true;
    } catch(_) {}
    try {
      if (summary && (summary.receipt_analysis_enabled === true || summary.receipts_analysis_enabled === true)) return true;
    } catch(_) {}
    return false;
  }

  function normalizeMappingObject(mapping){
    var out = { '0%':'', '6%':'', '13%':'', '17%':'', '24%':'' };
    if(!mapping || typeof mapping !== 'object') return out;
    if ('kat_fpa_a' in mapping || 'kat_fpa_b' in mapping || 'kat_fpa_g' in mapping || 'kat_fpa_d' in mapping || 'kat_fpa_e' in mapping) {
      out['0%'] = mapping.kat_fpa_a || '';
      out['6%'] = mapping.kat_fpa_b || '';
      out['13%'] = mapping.kat_fpa_g || '';
      out['17%'] = mapping.kat_fpa_d || '';
      out['24%'] = mapping.kat_fpa_e || '';
      return out;
    }
    Object.keys(out).forEach(function(key){
      if (Object.prototype.hasOwnProperty.call(mapping, key)) {
        out[key] = mapping[key] || '';
      }
    });
    return out;
  }

  function vatKeyFromValue(raw){
    var val = String(raw || '').trim().toLowerCase();
    if (!val) return '24%';
    if (val.indexOf('kat_fpa_a') >= 0) return '0%';
    if (val.indexOf('kat_fpa_b') >= 0) return '6%';
    if (val.indexOf('kat_fpa_g') >= 0) return '13%';
    if (val.indexOf('kat_fpa_d') >= 0) return '17%';
    if (val.indexOf('kat_fpa_e') >= 0) return '24%';
    if (val.indexOf('0') >= 0 && val.indexOf('24') === -1 && val.indexOf('13') === -1 && val.indexOf('17') === -1 && val.indexOf('6') === -1) return '0%';
    if (val.indexOf('6') >= 0) return '6%';
    if (val.indexOf('13') >= 0) return '13%';
    if (val.indexOf('17') >= 0) return '17%';
    if (val.indexOf('24') >= 0) return '24%';
    var numMatch = val.match(/(0|6|13|17|24)/);
    if (numMatch) {
      return String(numMatch[0]) + '%';
    }
    return '24%';
  }

  function mappingHasValues(map){
    if (!map) return false;
    return Object.keys(map).some(function(key){ return String(map[key] || '').trim() !== ''; });
  }

  function ensureSummaryCategoryFromLines(summary){
    if (!summary || typeof summary !== 'object') return;
    var lines = Array.isArray(summary.lines) ? summary.lines : [];
    var first = lines.find(function(line){ return line && String(line.category || '').trim(); });
    var cat = first ? String(first.category || '').trim() : '';
    if (cat) {
      if (!summary.category) summary.category = cat;
      if (!summary.characteristic) summary.characteristic = cat;
      if (!summary['χαρακτηρισμός']) summary['χαρακτηρισμός'] = cat;
    }
  }

  function summaryHasCompleteCategories(summary){
    if(!summary || typeof summary !== 'object') return false;
    var lines = Array.isArray(summary.lines) ? summary.lines : [];
    if(!lines.length){
      var headCat = String(summary.category || summary.characteristic || summary['χαρακτηρισμός'] || '').trim();
      var hasMtype = String(summary.mtype || summary.receipt_mtype || summary.receiptMtype || '').trim();
      var analysisFlag = !!(summary.receipt_analysis_enabled === true || summary.receiptAnalysisEnabled === true || summary.receipts_analysis_enabled === true);
      return !!(headCat || hasMtype || analysisFlag);
    }
    for(var i=0;i<lines.length;i++){
      var ln = lines[i] || {};
      if(!String(ln.category || '').trim()) return false;
    }
    var headCat = String(summary.category || summary.characteristic || summary['χαρακτηρισμός'] || '').trim();
    return !!headCat;
  }

  function hasBlockingWarnings(){
    try{
      var banner = document.getElementById('existingBanner');
      if (banner) {
        var cs = getComputedStyle(banner);
        if (cs.display !== 'none' && cs.visibility !== 'hidden' && cs.opacity !== '0') return true;
      }
      if(window.RC && typeof window.RC.hasWarnings === 'function'){
        return !!window.RC.hasWarnings();
      }
    }catch(_){ }
    return false;
  }

  // Β κατηγορία: δεν έχει G_CATEGORY_DATA.mtype_options (το MTYPE είναι μόνο για Γ κατηγορία)
  function isGCategoryCustomer(){
    try {
      var g = window.G_CATEGORY_DATA;
      return !!(g && g.mtype_options && g.mtype_options.length > 0);
    } catch(_) { return false; }
  }

  function detectActiveVat(){
    try {
      if (window._repeatModalVAT) return String(window._repeatModalVAT).trim();
    } catch(_) {}
    try {
      var hidden = document.querySelector('input[name="active_vat"]');
      if (hidden && hidden.value) return hidden.value.trim();
    } catch(_) {}
    try {
      var flagged = document.querySelector('[data-active-vat]');
      if (flagged) {
        var attr = flagged.getAttribute('data-active-vat') || flagged.dataset.activeVat || '';
        if (attr) return attr.trim();
      }
    } catch(_) {}
    try {
      if (typeof ACTIVE_VAT !== 'undefined' && ACTIVE_VAT) return String(ACTIVE_VAT).trim();
    } catch(_) {}
    return '';
  }

  function applyMappingToSummary(summary, map){
    if (!summary || typeof summary !== 'object') return false;
    var normalized = normalizeMappingObject(map || {});
    var lines = Array.isArray(summary.lines) ? summary.lines : [];
    var changed = false;
    lines.forEach(function(line){
      if (!line || typeof line !== 'object') return;
      var key = vatKeyFromValue(line.vat_category || line.vatCategory || line.vat || '');
      var mapped = normalized[key] || '';
      if (!String(line.category || '').trim() && mapped) {
        line.category = mapped;
        changed = true;
      }
    });
    if (changed) ensureSummaryCategoryFromLines(summary);
    return changed;
  }

  var receiptAnalysisCache = null;

  async function fetchRepeatEntryState(vat){
    var suffix = vat ? ('?vat=' + encodeURIComponent(vat)) : '';
    var endpoints = ['/api/repeat_entry/get_v2', '/api/repeat_entry/get'];
    for (var i = 0; i < endpoints.length; i++) {
      try {
        var resp = await fetch(endpoints[i] + suffix, { credentials: 'same-origin' });
        if (!resp.ok) continue;
        var data = await resp.json().catch(function(){ return null; });
        if (data) return data;
      } catch(_){ }
    }
    return null;
  }

  async function fetchReceiptProfileMapping(vat, profileName){
    try {
      var params = new URLSearchParams();
      params.set('mode', 'receipts');
      if (vat) params.set('vat', vat);
      var resp = await fetch('/api/char_profiles?' + params.toString(), { credentials:'same-origin' });
      if (!resp.ok) return null;
      var data = await resp.json().catch(function(){ return null; });
      if (!data) return null;
      var profiles = Array.isArray(data.profiles) ? data.profiles : [];
      var receiptsOnly = profiles.filter(function(p){
        var mode = String((p && p.mode) || '').trim().toLowerCase();
        return !mode || mode === 'receipts';
      }).map(function(p){
        return {
          name: String(p && p.name || '').trim(),
          mapping: normalizeMappingObject((p && (p.mapping || p.map)) || {}),
          receipt_mtype: String(p && (p.receipt_mtype || p.receiptMtype || p.invoice_mtype || p.invoiceMtype) || '').trim()
        };
      });
      var target = null;
      if (profileName) {
        target = receiptsOnly.find(function(p){ return p.name && p.name.toLowerCase() === profileName.toLowerCase(); });
      }
      if (!target) {
        target = receiptsOnly.find(function(p){ return !p.name; }) || receiptsOnly[0] || null;
      }
      return target;
    } catch(_){ return null; }
  }

  async function loadReceiptAnalysisMapping(){
    if (receiptAnalysisCache && (Date.now() - receiptAnalysisCache.ts) < 60000) {
      return receiptAnalysisCache.data;
    }
    var vat = detectActiveVat();
    var state = await fetchRepeatEntryState(vat);
    if (!state) return null;
    var repeat = state.repeat_entry || {};
    var mapping = normalizeMappingObject(repeat.mapping || {});
    var receiptMtype = String(repeat.receipt_mtype || '').trim();
    var profileName = String(repeat.profile_name || '').trim();
    if (!mappingHasValues(mapping) || profileName) {
      var profilePayload = await fetchReceiptProfileMapping(vat || state.vat || state.afm || '', profileName);
      if (profilePayload && mappingHasValues(profilePayload.mapping)) {
        mapping = profilePayload.mapping;
      }
      if (!receiptMtype && profilePayload && profilePayload.receipt_mtype) {
        receiptMtype = profilePayload.receipt_mtype;
      }
    }
    var out = { mapping: mapping, receipt_mtype: receiptMtype };
    receiptAnalysisCache = { ts: Date.now(), data: out };
    return out;
  }

  async function applyReceiptAnalysisProfile(summary){
    if (!isReceiptAnalysisContext(summary)) return;
    summary.receipt_analysis_enabled = true;
    var applied = false;
    try {
      if (window.RC && typeof window.RC.applySavedReceiptAnalysisMapping === 'function') {
        var result = await window.RC.applySavedReceiptAnalysisMapping(summary);
        applied = !!(result && result.applied);
      }
    } catch(err) {
      console.warn('RC.applySavedReceiptAnalysisMapping failed', err);
    }
    if (!applied) {
      try {
        var cache = await loadReceiptAnalysisMapping();
        if (cache && mappingHasValues(cache.mapping)) {
          applied = applyMappingToSummary(summary, cache.mapping);
          if (!summary.mtype && cache.receipt_mtype) {
            summary.mtype = cache.receipt_mtype;
            summary.receipt_mtype = cache.receipt_mtype;
          }
        }
      } catch(err2) {
        console.warn('receipt analysis mapping fallback failed', err2);
      }
    }
    ensureSummaryCategoryFromLines(summary);
  }
  function submitViaForm(s){
    var form=$id('saveSummaryForm');
    var input=$id('summaryJsonInput');
    if(form && input){
      input.value=JSON.stringify(s);
      if(typeof form.requestSubmit==='function') form.requestSubmit();
      else form.dispatchEvent(new Event('submit', { bubbles:true, cancelable:true }));
      return true;
    }
    return false;
  }
  function submitViaFetch(s){
    try{
      return fetch('/save_summary',{
        method:'POST',
        headers:{'content-type':'application/x-www-form-urlencoded'},
        body:'summary_json='+encodeURIComponent(JSON.stringify(s)),
        credentials:'same-origin'
      }).then(function(r){
        if(!r.ok) throw new Error('save failed');
        return r.text().then(function(txt){
          var body = String(txt || '');
          if (r.redirected || (r.url && r.url.indexOf('allow_edit_existing') !== -1) || body.indexOf('id="existingBanner"') !== -1) {
            throw new Error('reclassification_required');
          }
          return body;
        });
      });
    }catch(e){ return Promise.reject(e); }
  }
  function submitViaConfirmApi(s){
    try{
      var scrapeUrl = '';
      try {
        scrapeUrl = (($id('scrapeUrlField') && $id('scrapeUrlField').value) || ($id('scrapeUrlInput') && $id('scrapeUrlInput').value) || '').trim();
      } catch(_) {}

      return fetch('/api/confirm_receipt', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify({
          url: scrapeUrl,
          summary: s,
          force: false,
          category: (s && s.category) ? s.category : 'αποδειξακια',
          receipt_analysis_enabled: !!(s && (s.receipt_analysis_enabled || s.receipts_analysis_enabled))
        })
      }).then(function(r){
        return r.json().catch(function(){ return null; }).then(function(j){ return { r: r, j: j }; });
      }).then(function(out){
        if (out && out.j && (out.j.duplicate === true || out.j.allow_edit_existing === true)) {
          throw new Error('reclassification_required');
        }
        if(!out.r.ok || !out.j || !out.j.ok){
          throw new Error((out.j && out.j.error) ? out.j.error : ('save failed (' + out.r.status + ')'));
        }
        return out.j;
      });
    }catch(e){ return Promise.reject(e); }
  }

  function showReclassificationBanner(mark){
    var banner = document.getElementById('existingBanner');
    if (!banner) {
      var form = document.getElementById('markSearchForm');
      var host = (form && form.parentNode) ? form.parentNode : document.body;
      banner = document.createElement('div');
      banner.id = 'existingBanner';
      banner.className = 'mt-4 p-4 bg-yellow-50 rounded border border-yellow-300 text-yellow-800';
      banner.innerHTML = 'Το MARK <strong></strong> υπάρχει ήδη στο Excel. Θέλεις να τροποποιήσεις τον χαρακτηρισμό;'
        + '<div class="mt-2 flex gap-2">'
        + '<button id="forceEditBtn" type="button" class="bg-yellow-600 text-white px-3 py-2 rounded hover:bg-yellow-700">Επιβεβαίωση</button>'
        + '<button id="dismissBannerBtn" type="button" class="px-3 py-2 border rounded hover:bg-gray-50">Άκυρο</button>'
        + '</div>';
      if (form && form.parentNode) host.insertBefore(banner, form.nextSibling);
      else host.prepend(banner);
    }

    var strong = banner.querySelector('strong');
    if (strong) strong.textContent = String(mark || '').trim() || '?';

    var dismissBtn = banner.querySelector('#dismissBannerBtn');
    if (dismissBtn) dismissBtn.onclick = function(){ banner.style.display = 'none'; };

    var forceBtn = banner.querySelector('#forceEditBtn');
    if (forceBtn) forceBtn.onclick = function(){
      var base = (window.SEARCH_BASE_URL || '/search');
      var mv = encodeURIComponent(String(mark || document.getElementById('markInput')?.value || '').trim());
      if (!mv) return;
      window.location = base + '?mark=' + mv + '&force_edit=1';
    };

    banner.style.display = 'block';
    try {
      var modal = document.getElementById('summaryModal');
      if (modal) modal.style.display = 'none';
    } catch(_) {}
  }
  async function afterSubmit(mark, dedupeKey){
    lsSet('UI:useReceipts','1');
    if (dedupeKey) ssDel(dedupeKey);
    try{
      var successMsg = 'Αποθηκεύτηκε η απόδειξη (repeat).';
      if (window.persistReceiptFlash) window.persistReceiptFlash(successMsg, 'success');

      try { window.__RC_CLEAR_MARK_AFTER_SAVE = true; } catch(_){}
      try {
        if (typeof window.clearSearchInputs === 'function') window.clearSearchInputs();
      } catch(_){}
      try {
        if (typeof window.clearReceiptSearchCacheOnClose === 'function') window.clearReceiptSearchCacheOnClose();
      } catch(_){}

      var urlInput = $id('scrapeUrlInput');
      if (urlInput) {
        urlInput.value = '';
        try { urlInput.dispatchEvent(new Event('input', { bubbles: true })); } catch(_){}
      }
      var markInput = $id('markInput');
      if (markInput) {
        markInput.value = '';
        try { markInput.dispatchEvent(new Event('input', { bubbles: true })); } catch(_){}
      }

      var modal = $id('summaryModal');
      if (modal) modal.style.display = 'none';

      var reloaded = false;
      if (typeof window.partiallyReloadInvoiceTable === 'function') {
        try { reloaded = !!(await window.partiallyReloadInvoiceTable()); } catch(_) { reloaded = false; }
      }
      if (!reloaded) {
        try {
          var tableRes = await fetch('/list/fragment', { method: 'GET', credentials: 'same-origin' });
          if (tableRes.ok) {
            var data = await tableRes.json().catch(function(){ return null; });
            var container = document.getElementById('summary-container');
            if (data && data.ok && data.table_html && container) {
              container.innerHTML = data.table_html;
              if (typeof window.FBP_INIT_TABULATOR === 'function') window.FBP_INIT_TABULATOR();
              else if (typeof window.FBP_INIT_TABLE === 'function') window.FBP_INIT_TABLE();
              reloaded = true;
            }
          }
        } catch(_){}
      }

      if (window.showFlash) {
        if (reloaded) window.showFlash(successMsg, 'success', 4200);
        else window.showFlash('Η αποθήκευση ολοκληρώθηκε, αλλά δεν έγινε ανανέωση πίνακα. Πάτησε αναζήτηση ή ανανέωση λίστας.', 'warning', 4500);
      }
    }catch(_){ }
    // Απελευθέρωσε τη φρουρά Β κατηγορίας
    try { window.__RC_B_CAT_AUTOSAVE_IN_PROGRESS = false; } catch(_) {}
  }
  var trying=false;
  async function tryDirect(){
    if(trying) return;
    if(!isReceipts()||!isRepeat()) return;
    if(!isMixedMode()) return;
    try {
      var q = new URLSearchParams(window.location.search || '');
      if (q.get('allow_edit_existing') === '1' && q.get('force_edit') !== '1') return;
    } catch(_) {}
    if(hasBlockingWarnings()) return;

    var s=parseSummary();
    if(!s||!isReceiptSummary(s)||!hasLines(s)) return;

    var mark=String(s.mark||s.MARK||'').trim();
    if(!mark) return;
    var k=onceKeyFromSummary(s);
    var prevTs = parseInt(ssGet(k) || '0', 10);
    var nowTs = Date.now();
    // anti-double-submit window, but do not block forever
    if(prevTs && !isNaN(prevTs) && (nowTs - prevTs) < 4000) return;
    ssSet(k, String(nowTs));
    trying=true;

    function abortAttempt(){
      trying = false;
      ssDel(k);
    }

    s = normalizeReceipt(s);
    var analysisActive = isAnalysisMode() || isReceiptAnalysisContext(s);

    // Merge live component state (`summaryDataInput`) so MTYPE set in the component is not lost
    try {
      var compEl = document.getElementById('summaryDataInput');
      if (compEl && compEl.value && compEl.value !== '{}' ) {
        try {
          var comp = JSON.parse(compEl.value || '{}') || {};
          if (comp && typeof comp === 'object') {
            if (comp.mtype) s.mtype = comp.mtype;
            if (comp.receipt_mtype) s.receipt_mtype = comp.receipt_mtype;
            if (comp.invoice_mtype) s.invoice_mtype = comp.invoice_mtype;
            if (comp.receiptMtype) s.receipt_mtype = comp.receiptMtype;
            if (comp.invoiceMtype) s.invoice_mtype = comp.invoiceMtype;
            if (comp.lines) s.lines = comp.lines;
          }
        } catch(e){ /* ignore parse errors */ }
      }

      // If still missing, try localStorage / cached repeat_entry as a fallback —
      // this covers the UX where the user saved the "Κωδικός Κίνησης Αποδείξεων"
      // but the modal/component state wasn't mirrored into #summaryDataInput yet.
      if ((!s.mtype || s.mtype === '') && (!s.receipt_mtype || s.receipt_mtype === '')) {
        try {
          var saved = (window.__cachedReceiptMtype || null) || (localStorage && localStorage.getItem && localStorage.getItem('receipt_mtype')) || null;
          if (saved) {
            s.mtype = s.mtype || saved;
            s.receipt_mtype = s.receipt_mtype || saved;
          }
        } catch(_) { /* ignore localStorage */ }
      }
    } catch(e) { /* ignore */ }

    try {
      await applyReceiptAnalysisProfile(s);
    } catch(errApply) {
      console.warn('receipt analysis apply failed', errApply);
    }

    // Για Β κατηγορία: ενημέρωσε το summaryJsonInput με τον χαρακτηρισμό από το αποθηκευμένο προφίλ
    // ώστε το summary modal να τον εμφανίζει σωστά.
    if (!isGCategoryCustomer()) {
      var _bCatInp = $id('summaryJsonInput');
      if (_bCatInp) {
        _bCatInp.value = JSON.stringify(s);
        try { if (window.RC_forcePopulateSummaryModal) window.RC_forcePopulateSummaryModal(); } catch(_) {}
      }
    }

    analysisActive = isAnalysisMode() || isReceiptAnalysisContext(s);
    if(!summaryHasCompleteCategories(s)){
      abortAttempt();
      return;
    }
    // Ο έλεγχος MTYPE ισχύει ΜΟΝΟ για Γ κατηγορία — η Β κατηγορία δεν χρησιμοποιεί MTYPE
    if(analysisActive && isGCategoryCustomer()){
      var activeMtype = String(s.mtype || s.receipt_mtype || '').trim();
      if(!activeMtype){
        abortAttempt();
        return;
      }
    }
    if(hasBlockingWarnings()){
      abortAttempt();
      return;
    }

    // Για Β κατηγορία: σήκωσε φρουρά ώστε το repeat_flow_guard.js να μην στείλει δεύτερη αίτηση
    if (!isGCategoryCustomer()) window.__RC_B_CAT_AUTOSAVE_IN_PROGRESS = true;

    try {
      await submitViaConfirmApi(s);
      await afterSubmit(mark, k);
      return;
    } catch(err){
      try { window.__RC_B_CAT_AUTOSAVE_IN_PROGRESS = false; } catch(_) {}
      var errMsg = String((err && err.message) || '').toLowerCase();
      if (errMsg.indexOf('reclassification_required') !== -1 || errMsg.indexOf('already') !== -1 || errMsg.indexOf('υπάρ') !== -1 || errMsg.indexOf('exist') !== -1) {
        trying = false;
        ssDel(k);
        showReclassificationBanner(mark);
        try { if (window.showFlash) window.showFlash('Το MARK ' + mark + ' υπάρχει ήδη στο Excel.', 'warning', 4500); } catch(_) {}
        return;
      }
      if(submitViaForm(s)){
        setTimeout(function(){
          trying=false;
          ssDel(k);
          try { window.__RC_B_CAT_AUTOSAVE_IN_PROGRESS = false; } catch(_) {}
        }, 2500);
        return;
      }
      try {
        await submitViaFetch(s);
        await afterSubmit(mark, k);
        return;
      } catch(err2){
        trying=false;
        ssDel(k);
        try { window.__RC_B_CAT_AUTOSAVE_IN_PROGRESS = false; } catch(_) {}
        try{
          var msg = (err2 && err2.message) ? err2.message : ((err && err.message) ? err.message : 'server');
          var errMsg = 'Σφάλμα αποθήκευσης: ' + msg;
          if (window.showFlash) window.showFlash(errMsg, 'error', 6000);
          if (window.persistReceiptFlash) window.persistReceiptFlash(errMsg, 'error');
        }catch(_){ }
      }
    }
  }

  function patchOpenModal(){
    var old = window.openModal;
    window.openModal = function(id){
      if(id==='summaryModal' && isReceipts() && isRepeat() && isMixedMode()){
        tryDirect();
        return;
      }
      if(typeof old==='function') return old.apply(this, arguments);
    };
  }

  document.addEventListener('DOMContentLoaded', function(){
    try{
      if(lsGet('UI:useReceipts','0')==='1'){
        var sw=$id('useReceiptsSwitch');
        if(sw && !sw.checked){ sw.checked=true; try{ sw.dispatchEvent(new Event('change',{bubbles:true})); }catch(_){ } }
      }
    }catch(_){}

    tryDirect();
    setTimeout(tryDirect, 0);
    setTimeout(tryDirect, 250);
    setTimeout(tryDirect, 750);

    var el=$id('summaryJsonInput');
    if(el){
      var prev=el.value;
      setInterval(function(){
        if(el.value!==prev){ prev=el.value; tryDirect(); }
      }, 200);
    }

    var r1=$id('useReceiptsSwitch'), r2=$id('repeatEntrySwitch');
    if(r1) r1.addEventListener('change', function(){ if(this.checked) setTimeout(tryDirect,10); });
    if(r2) r2.addEventListener('change', function(){ if(this.checked) setTimeout(tryDirect,10); });

    patchOpenModal();
  });
})();
