
/*! repeat_flow_guard.js
 * Purpose:
 *  - Stop infinite auto-submit/refresh loop after saving repeat settings or reloading.
 *  - Auto-apply repeat for RECEIPTS (category = 'αποδειξακια') WITHOUT opening modal,
 *    when repeat is enabled and NOT in reclassification mode (force_edit=1).
 *  - Never submit empty/meaningless summaries.
 *  - Minimal, self-contained. Safe to include multiple times (guards internal).
 */

(function() {
  if (window.__REPEAT_FLOW_GUARD_ATTACHED__) return;
  window.__REPEAT_FLOW_GUARD_ATTACHED__ = true;

  // --- Utility: query param --------------------------------------------------
  function getQueryParam(name){
    try{
      const u = new URL(window.location.href);
      return u.searchParams.get(name);
    }catch(_){ return null; }
  }
  const FORCE_EDIT = (getQueryParam('force_edit') === '1');

  // --- Utility: meaningful summary check ------------------------------------
  function isMeaningfulSummaryObject(obj){
    try{
      if (!obj || typeof obj !== 'object') return false;
      const mark = String(obj.mark || obj.MARK || '').trim();
      if (!/^\d{15}$/.test(mark)) return false;

      // If totals exist, accept
      const totalValue = String(obj.totalValue || obj.total_amount || '').trim();
      if (totalValue) return true;

      let lines = [];
      if (Array.isArray(obj.lines)) lines = obj.lines;
      else if (obj.lines && typeof obj.lines === 'object') {
        try { lines = Object.values(obj.lines).filter(Boolean); } catch(_) { lines = []; }
      }
      if ((!lines || !lines.length) && obj.raw && Array.isArray(obj.raw.lines)) {
        lines = obj.raw.lines;
      }
      if (lines.length === 0) return false;
      // at least one line with any info
      const hasInfo = lines.some(l => {
        if (!l) return false;
        const desc = (l.description || l.desc || '').toString().trim();
        const amt  = (l.amount || l.lineTotal || l.total || '').toString().trim();
        const id   = (l.id || l.line_id || '').toString().trim();
        return !!(desc || amt || id);
      });
      return hasInfo;
    }catch(_){ return false; }
  }

  // --- Utility: is receipt summary ------------------------------------------
  function isReceiptSummary(obj){
    try{
      if (!obj || typeof obj !== 'object') return false;
      if (obj.is_receipt === true) return true;
      const tname = (obj.type_name || obj.type || '').toString().toLowerCase();
      const cat   = (obj.category || obj.characteristic || obj['χαρακτηρισμός'] || '').toString().toLowerCase();
      if (tname.includes('απόδει') || tname.includes('receipt') || cat === 'αποδειξακια') return true;
      // heuristic: only totalValue w/o net/vat commonly from receipts
      if (!obj.totalVatAmount && !obj.totalNetValue && obj.totalValue) return true;
      return false;
    }catch(_){ return false; }
  }

  // --- Get DOM refs safely ---------------------------------------------------
  function byId(id){ return document.getElementById(id); }

  // --- Repeat enabled fetch/cache -------------------------------------------
  let repeatEnabled = null;   // null=unknown, true/false=server state
  let repeatMapping = {};
  // store mark for which cash-warning dialog has been shown to avoid spam
  let cashWarningShownMark = null;
  function fetchRepeatConfigOnce(){
    if (repeatEnabled !== null) return; // already fetched
    try{
      fetch('/api/repeat_entry/get', { credentials: 'same-origin' })
        .then(r => r.ok ? r.json() : null)
        .then(j => {
          if (j && j.ok) {
            repeatEnabled = !!(j.enabled);
            repeatMapping = j.mapping || {};
          } else {
            // fallback to switch state if present
            const sw = byId('repeatEntrySwitch');
            repeatEnabled = sw ? !!sw.checked : false;
          }
        })
        .catch(_ => {
          const sw = byId('repeatEntrySwitch');
          repeatEnabled = sw ? !!sw.checked : false;
        });
    }catch(_){}
  }

  // call once at start
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', fetchRepeatConfigOnce);
  } else {
    fetchRepeatConfigOnce();
  }

  // --- Guard: stop empty search submit --------------------------------------
  function installEmptySearchGuard(){
    const form = byId('markSearchForm');
    if (!form) return;
    form.addEventListener('submit', (e) => {
      const mark = (byId('markInput')?.value || '').trim();
      const url  = (byId('scrapeUrlInput')?.value || '').trim();
      if (!mark && !url) {
        e.preventDefault();
      }
    }, true);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installEmptySearchGuard);
  } else {
    installEmptySearchGuard();
  }

  // --- One-shot submit blocker (prevents loops) ------------------------------
  let autoSaveInProgress = false;
  let autoSaveWatchdog = null;
  function resetAutoSaveState(){
    autoSaveInProgress = false;
    if (autoSaveWatchdog) {
      try { clearTimeout(autoSaveWatchdog); } catch(_) {}
      autoSaveWatchdog = null;
    }
  }
  function safeSubmitSaveSummary(){
    if (autoSaveInProgress) return false;
    const form = byId('saveSummaryForm');
    if (!form) return false;
    autoSaveInProgress = true;
    try {
      if (typeof form.requestSubmit === 'function') {
        form.requestSubmit();
      } else {
        form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      }
    } catch(_) { autoSaveInProgress = false; return false; }
    // release guard soon after submit pipeline starts
    setTimeout(() => { autoSaveInProgress = false; }, 1200);
    return true;
  }

  try {
    window.__repeatFlowResetAutoSave = resetAutoSaveState;
    window.addEventListener('scanmydata:save-summary-finished', resetAutoSaveState);
    window.addEventListener('scanmydata:reclassification-cancelled', resetAutoSaveState);
  } catch(_) {}

  // --- Apply receipt auto-category + submit ---------------------------------
  function applyReceiptAutoCategoryAndSubmit(){
    const input = byId('summaryJsonInput');
    if (!input) return false;
    let data = null;
    try { data = JSON.parse(input.value || '{}'); } catch(_){ data = null; }
    if (!isMeaningfulSummaryObject(data)) return false;

    // force receipt flags
    data.is_receipt = true;
    data.category   = 'αποδειξακια';
    // also set greek key to be safe
    if (!data['χαρακτηρισμός']) data['χαρακτηρισμός'] = 'αποδειξακια';
    if (!data.type_name) data.type_name = 'Απόδειξη';
    // do NOT touch per-line categories (receipts don't need per-line classification)

    input.value = JSON.stringify(data);
    return safeSubmitSaveSummary();
  }

  // --- Observe summary becoming meaningful, then decide modal/auto-submit ---
  function watchSummaryAndEnforceReceiptsRepeat(){
    const input = byId('summaryJsonInput');
    const modal = byId('summaryModal');
    if (!input) return;

    function decideAndAct(){
      if (autoSaveInProgress) return;
      // Αν η Β-κατηγορία έχει ήδη ξεκινήσει αυτόματη αποθήκευση, μην έχει διπλή υποβολή
      if (window.__RC_B_CAT_AUTOSAVE_IN_PROGRESS) return;
      let obj = null;
      try { obj = JSON.parse(input.value || '{}'); } catch(_){ obj = null; }
      if (!isMeaningfulSummaryObject(obj)) return;

      // if NOT reclassification and repeat is enabled and summary is a receipt -> auto-submit (no modal)
      const receiptsSwitchOn = !!(byId('useReceiptsSwitch') && byId('useReceiptsSwitch').checked);

      // if repeatEnabled is still unknown, peek the switch; else use server state
      const repeatOn = (repeatEnabled === null)
        ? !!(byId('repeatEntrySwitch') && byId('repeatEntrySwitch').checked)
        : !!repeatEnabled;

      // --- invoice cash/mtype mismatch check during repeat --------------
      if (!FORCE_EDIT && repeatOn && !isReceiptSummary(obj)) {
        const paymentMethodType = String(obj.paymentMethodType || '').trim();
        const selectedMtype = String(obj.mtype || obj.invoice_mtype || '').trim();
        function getCashCode(){
          if (window.G_CATEGORY_DATA && window.G_CATEGORY_DATA.cash_mtype_code) {
            return String(window.G_CATEGORY_DATA.cash_mtype_code).trim();
          }
          const sel = document.getElementById('invoiceMtypeSelect');
          if (sel) {
            for (const o of sel.options) {
              const lbl = String(o.textContent||'').toLowerCase();
              if (lbl.includes('αγορ') && lbl.includes('εξοδ') && lbl.includes('ταμει')) {
                return String(o.value||'').trim();
              }
            }
          }
          return '';
        }
        const cashCode = getCashCode();
        if (paymentMethodType === '3' && selectedMtype && cashCode && selectedMtype !== cashCode) {
          if (cashWarningShownMark !== obj.mark) {
            cashWarningShownMark = obj.mark;
            showModalConfirm('Προειδοποίηση',
              `Τρόπος πληρωμής μετρητά αλλά Είδος Κίνησης=${selectedMtype} (αναμένεται ${cashCode}). Θέλεις αλλαγή;`,
              'Αλλαγή τώρα','Συνέχεια').then(user => {
                if (user) {
                  obj.mtype = obj.invoice_mtype = cashCode;
                  try { input.value = JSON.stringify(obj); } catch(_){}
                }
                safeSubmitSaveSummary();
              });
          }
          return; // pause repeat until user responds
        }
      }

      if (!FORCE_EDIT && repeatOn && (receiptsSwitchOn || isReceiptSummary(obj))) {
        // hide modal if already shown
        if (modal) try { modal.style.display = 'none'; } catch(_){}
        applyReceiptAutoCategoryAndSubmit();
        return;
      }
      // otherwise do nothing (existing code can open modal for invoices, or for receipts when repeat off)
    }

    // decide now and also on changes
    decideAndAct();

    // Watch for value changes on summary input
    let last = input.value;
    setInterval(function(){
      const cur = input.value;
      if (cur !== last) { last = cur; decideAndAct(); }
    }, 250);

    // Also react when modal becomes visible
    if (modal && window.MutationObserver) {
      const mo = new MutationObserver(function(muts){
        for (const m of muts) {
          if (m.type === 'attributes' && m.attributeName === 'style') decideAndAct();
        }
      });
      try { mo.observe(modal, { attributes: true, attributeFilter: ['style'] }); } catch(_){}
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', watchSummaryAndEnforceReceiptsRepeat);
  } else {
    watchSummaryAndEnforceReceiptsRepeat();
  }

  // --- Safety: never auto-submit empty summary from any other auto-run code --
  // Intercept form submit (capture) and stop if summary is not meaningful.
  function installSaveSummaryFormGuard(){
    const form = byId('saveSummaryForm');
    if (!form) return;
    form.addEventListener('submit', function(e){
      try {
        const input = byId('summaryJsonInput');
        if (!input) return;
        let obj = null;
        try { obj = JSON.parse(input.value || '{}'); } catch(_){ obj = null; }
        if (!isMeaningfulSummaryObject(obj)) {
          e.preventDefault();
          e.stopPropagation();
          return false;
        }
      } catch(_) {}
    }, true);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', installSaveSummaryFormGuard);
  } else {
    installSaveSummaryFormGuard();
  }

})();
