/*! receipts_fast_flow.js
 * Seamless AJAX receipt flow when repeat mode is enabled
 * - No page reloads (scrape + save via AJAX)
 * - Respects all existing validation (checks for existing MARK, shows banner if needed)
 * - Auto-enabled when repeat + receipts mode both ON
 * - Uses same /save_summary endpoint as form (preserves all server-side logic)
 * 
 * NO USER BUTTON - automatically activates when repeat mode enabled
 */
(function(){
  if (window.__RC_USE_CANONICAL_RECEIPT_AUTOCONFIRM === true) return;
  if (window.__FAST_FLOW_ATTACHED__) return;
  window.__FAST_FLOW_ATTACHED__ = true;

  // ===== Helpers =====
  const $ = (sel) => document.querySelector(sel);
  const $id = (id) => document.getElementById(id);
  const lsGet = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch(_) { return d; } };

  function isRepeatEnabled() {
    try { const sw = $id('repeatEntrySwitch'); if (sw) return !!sw.checked; } catch(_) {}
    return lsGet('REPEAT:enabled', '0') === '1';
  }

  function isReceiptsMode() {
    try { const sw = $id('useReceiptsSwitch'); if (sw) return !!sw.checked; } catch(_) {}
    return lsGet('UI:useReceipts', '0') === '1';
  }

  function getRepeatMapping() {
    try {
      const m = lsGet('REPEAT:mapping', '{}');
      return JSON.parse(m);
    } catch(_) { return {}; }
  }

  function showLoadingOverlay(msg = 'Σάρωση...') {
    let overlay = $id('fastFlowOverlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'fastFlowOverlay';
      document.body.appendChild(overlay);
    }
    overlay.innerHTML = `
      <div class="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
        <div class="bg-white rounded-lg shadow-lg p-6 max-w-sm">
          <div class="flex items-center gap-3">
            <div class="animate-spin text-2xl">⏳</div>
            <div class="text-gray-700">${msg}</div>
          </div>
        </div>
      </div>
    `;
    overlay.style.display = 'block';
    return overlay;
  }

  function hideLoadingOverlay() {
    const overlay = $id('fastFlowOverlay');
    if (overlay) overlay.style.display = 'none';
  }

  function showFlash(msg, type = 'info', duration = 3000) {
    if (window.showFlash) {
      window.showFlash(msg, type, duration);
    } else {
      console.log('[' + type.toUpperCase() + ']', msg);
    }
  }

  function showExistingBanner(mark) {
    // Shows the yellow "already exists" banner and hides modal
    const banner = $id('existingBanner');
    if (banner) {
      banner.style.display = 'block';
      const modal = $id('summaryModal');
      if (modal) modal.style.display = 'none';
      return true;
    }
    return false;
  }

  function getModalElement() {
    return $id('summaryModal') || $('.modal-summary');
  }

  function showModal() {
    const modal = getModalElement();
    if (!modal) return false;
    modal.style.display = 'block';
    try { modal.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch(_) {}
    return true;
  }

  function hideModal() {
    const modal = getModalElement();
    if (modal) modal.style.display = 'none';
  }

  function escapeHtml(text) {
    const map = {
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
    };
    return String(text).replace(/[&<>"']/g, m => map[m]);
  }

  // ===== Core AJAX flow =====
  async function scrapeReceiptViaAjax(url) {
    showLoadingOverlay('Σάρωση παραστατικού...');

    try {
      const res = await fetch('/api/scrape_receipt', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ url: url })
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.error || `HTTP ${res.status}`);
      }

      const data = await res.json();
      if (!data.ok) {
        throw new Error(data.error || 'Unknown scraper error');
      }

      hideLoadingOverlay();
      return data.raw || data;
    } catch (err) {
      hideLoadingOverlay();
      showFlash('❌ Σφάλμα σάρωσης: ' + err.message, 'error', 5000);
      throw err;
    }
  }

  function populateModalWithReceipt(receipt) {
    try {
      const summaryInput = $id('summaryJsonInput');
      if (summaryInput) {
        summaryInput.value = JSON.stringify(receipt);
      }

      // Apply repeat mapping if enabled
      if (isRepeatEnabled()) {
        const mapping = getRepeatMapping();
        if (mapping && typeof mapping === 'object') {
          const categoryInput = $id('summaryCategory');
          if (categoryInput && mapping.category) {
            categoryInput.value = mapping.category;
          }
          const charInput = $id('summaryCharacteristic');
          if (charInput && mapping.characteristic) {
            charInput.value = mapping.characteristic;
          }
        }
      }

      return true;
    } catch (err) {
      console.warn('Could not populate modal:', err.message);
      return false;
    }
  }

  async function submitReceiptViaAjax(receipt) {
    showLoadingOverlay('Αποθήκευση παραστατικού...');

    try {
      // Use /save_summary like the normal form does (preserves all server validation)
      const formData = new FormData();

      // Prefer the live value from the legacy hidden input if present — user may have
      // changed MTYPE inside the modal after the initial scrape payload was set.
      let payload = receipt;
      try {
        const legacy = document.getElementById('summaryJsonInput');
        if (legacy && legacy.value && String(legacy.value).trim() !== '') {
          const parsed = JSON.parse(legacy.value);
          if (parsed && typeof parsed === 'object') payload = parsed;
        }
      } catch (err) {
        /* ignore and fallback to original scraped receipt */
      }

      // Merge any live component state (`summaryDataInput`) — copy invoice/receipt mtype and lines
      try {
        const compEl = document.getElementById('summaryDataInput');
        if (compEl && compEl.value && String(compEl.value).trim() !== '') {
          const comp = JSON.parse(compEl.value || '{}') || {};
          if (comp && typeof comp === 'object') {
            // prefer explicit fields from component
            if (comp.mtype) payload.mtype = comp.mtype;
            if (comp.receipt_mtype) payload.receipt_mtype = comp.receipt_mtype;
            if (comp.invoice_mtype) payload.invoice_mtype = comp.invoice_mtype;
            if (comp.receiptMtype) payload.receipt_mtype = comp.receiptMtype;
            if (comp.invoiceMtype) payload.invoice_mtype = comp.invoiceMtype;
            if (comp.lines) payload.lines = comp.lines;
          }
        }

        // fallback: use locally-saved receipt MTYPE (or cached backend value)
        if ((!payload.mtype || payload.mtype === '') && (!payload.receipt_mtype || payload.receipt_mtype === '')) {
          try {
            const saved = (window.__cachedReceiptMtype || null) || (localStorage && localStorage.getItem && localStorage.getItem('receipt_mtype')) || null;
            if (saved) {
              payload.mtype = payload.mtype || saved;
              payload.receipt_mtype = payload.receipt_mtype || saved;
            }
          } catch(_) { /* ignore */ }
        }
      } catch (err) { /* defensive - do not block save */ }

      // debug: ensure payload.mtype present when user selected one
      try { console.debug('[fast-flow] submitting summary.mtype=', payload.mtype || payload.receipt_mtype || payload.invoice_mtype || ''); } catch(_){}

      // Guard: if a visible MTYPE selector exists in modal, require selection before autosave.
      try {
        const invCont = document.getElementById('invoiceMtypeContainer');
        const invSel = document.getElementById('invoiceMtypeSelect');
        const recCont = document.getElementById('receiptMtypeContainerSummary');
        const recSel = document.getElementById('receiptMtypeSelectSummary');
        const invVisible = !!(invCont && window.getComputedStyle(invCont).display !== 'none');
        const recVisible = !!(recCont && window.getComputedStyle(recCont).display !== 'none');
        const invMissing = invVisible && (!invSel || !String(invSel.value || '').trim());
        const recMissing = recVisible && (!recSel || !String(recSel.value || '').trim());
        if (invMissing || recMissing) {
          hideLoadingOverlay();
          const msg = invMissing
            ? 'Συμπλήρωσε το Είδος Κίνησης (MTYPE) πριν την αποθήκευση.'
            : 'Συμπλήρωσε το Είδος Κίνησης για τις αποδείξεις πριν την αποθήκευση.';
          if (typeof window.showModalAlert === 'function') await window.showModalAlert('Ελλιπή πεδία', msg);
          else showFlash(msg, 'warning', 3500);
          return false;
        }
      } catch(_) {}

      formData.append('summary_json', JSON.stringify(payload));
      formData.append('ajax', '1'); // Force JSON response; prevents server redirect

      const res = await fetch('/save_summary', {
        method: 'POST',
        body: formData,
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'XMLHttpRequest' }
      });

      hideLoadingOverlay();

      const j = await res.json().catch(() => null);
      if (!res.ok || !j || !j.ok) {
        const errMsg = (j && (j.error || j.message)) ? String(j.error || j.message) : ('Σφάλμα αποθήκευσης (HTTP ' + res.status + ')');
        showFlash('❌ ' + errMsg, 'error', 5000);
        return false;
      }

      // Success: clear search inputs/cache and refresh table fragment.
      try { window.__RC_CLEAR_MARK_AFTER_SAVE = true; } catch(_) {}

      const urlInput = $id('scrapeUrlInput');
      if (urlInput) {
        urlInput.value = '';
        try { urlInput.dispatchEvent(new Event('input', { bubbles: true })); } catch(_) {}
      }

      const markInput = $id('markInput');
      if (markInput) {
        markInput.value = '';
        try { markInput.dispatchEvent(new Event('input', { bubbles: true })); } catch(_) {}
      }

      try { if (typeof window.clearSearchInputs === 'function') window.clearSearchInputs(); } catch(_) {}
      try { if (typeof window.clearReceiptSearchCacheOnClose === 'function') window.clearReceiptSearchCacheOnClose(); } catch(_) {}

      hideModal();

      let reloaded = false;
      if (typeof window.partiallyReloadInvoiceTable === 'function') {
        try { reloaded = !!(await window.partiallyReloadInvoiceTable()); } catch(_) { reloaded = false; }
      }

      // Fallback for repeat mode: force-refresh table fragment even if helper returns false.
      if (!reloaded) {
        try {
          const tableRes = await fetch('/list/fragment', { method: 'GET', credentials: 'same-origin' });
          if (tableRes.ok) {
            const data = await tableRes.json().catch(() => null);
            const container = document.getElementById('summary-container');
            if (data && data.ok && data.table_html && container) {
              container.innerHTML = data.table_html;
              if (typeof window.FBP_INIT_TABULATOR === 'function') window.FBP_INIT_TABULATOR();
              else if (typeof window.FBP_INIT_TABLE === 'function') window.FBP_INIT_TABLE();
              reloaded = true;
            }
          }
        } catch(_) {}
      }

      if (reloaded) showFlash('✓ Αποθηκεύτηκε η απόδειξη', 'success', 2500);
      else showFlash('Η αποθήκευση ολοκληρώθηκε, αλλά δεν έγινε ανανέωση πίνακα. Πάτησε αναζήτηση ή ανανέωση λίστας.', 'warning', 4500);

      return true;
    } catch (err) {
      hideLoadingOverlay();
      showFlash('❌ Σφάλμα αποθήκευσης: ' + err.message, 'error', 5000);
      return false;
    }
  }

  // ===== Form submit handler (auto-activates when repeat + receipts enabled) =====
  function hookFormSubmit() {
    const form = $id('markSearchForm');
    if (!form) return;

    // Capture original submit handler
    const originalHandler = form.onsubmit;

    form.addEventListener('submit', async function(evt) {
      // Only activate if BOTH repeat AND receipts mode enabled
      if (!isRepeatEnabled() || !isReceiptsMode()) {
        // Use normal flow
        return;
      }

      evt.preventDefault();

      const urlInput = $id('scrapeUrlInput');
      const url = urlInput ? urlInput.value.trim() : '';

      if (!url) {
        showFlash('⚠️ Παρακαλώ εισάγετε URL', 'warning', 2000);
        return;
      }

      try {
        // Step 1: Scrape via AJAX
        const receipt = await scrapeReceiptViaAjax(url);
        if (!receipt) {
          showFlash('❌ Δεν ήταν δυνατή η σάρωση', 'error', 3000);
          return;
        }

        // Step 2: Show modal with scraped data
        populateModalWithReceipt(receipt);
        showModal();

        // Step 3: Auto-submit if repeat enabled (which we know it is)
        setTimeout(() => {
          submitReceiptViaAjax(receipt).catch(err => {
            console.error('Auto-submit failed:', err);
          });
        }, 600);
      } catch (err) {
        console.error('Fast flow error:', err);
      }
    }, { capture: false });
  }

  // ===== Initialize =====
  function init() {
    hookFormSubmit();
    console.log('[fast-flow] Initialized - will activate when repeat + receipts mode enabled');
  }

  // Wait for DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
