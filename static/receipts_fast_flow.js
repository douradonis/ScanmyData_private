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
    // Shows (or creates) the yellow "already exists" banner and hides modal
    let banner = $id('existingBanner');

    if (!banner) {
      const form = $id('markSearchForm');
      const host = form && form.parentNode ? form.parentNode : document.body;
      banner = document.createElement('div');
      banner.id = 'existingBanner';
      banner.className = 'mt-4 p-4 bg-yellow-50 rounded border border-yellow-300 text-yellow-800';
      banner.innerHTML = `
        Το MARK <strong></strong> υπάρχει ήδη στο Excel. Θέλεις να τροποποιήσεις τον χαρακτηρισμό;
        <div class="mt-2 flex gap-2">
          <button id="forceEditBtn" type="button" class="bg-yellow-600 text-white px-3 py-2 rounded hover:bg-yellow-700">Επιβεβαίωση</button>
          <button id="dismissBannerBtn" type="button" class="px-3 py-2 border rounded hover:bg-gray-50">Άκυρο</button>
        </div>
      `;
      if (form && form.parentNode) host.insertBefore(banner, form.nextSibling);
      else host.prepend(banner);
    }

    const strong = banner.querySelector('strong');
    if (strong) strong.textContent = String(mark || '').trim() || '?';

    const dismissBtn = banner.querySelector('#dismissBannerBtn');
    if (dismissBtn) {
      dismissBtn.onclick = function() {
        banner.style.display = 'none';
      };
    }

    const forceBtn = banner.querySelector('#forceEditBtn');
    if (forceBtn) {
      forceBtn.onclick = function() {
        const markRaw = String(mark || $id('markInput')?.value || '').trim();
        if (!markRaw) return;
        if (typeof window.activateReclassificationWithoutReload === 'function' && window.activateReclassificationWithoutReload(markRaw)) {
          return;
        }
        const base = (window.SEARCH_BASE_URL || '/search');
        window.location = base + '?mark=' + encodeURIComponent(markRaw) + '&force_edit=1';
      };
    }

    banner.style.display = 'block';
    const modal = $id('summaryModal');
    if (modal) modal.style.display = 'none';
    return true;
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

      formData.append('summary_json', JSON.stringify(payload));

      const res = await fetch('/save_summary', {
        method: 'POST',
        body: formData,
        credentials: 'same-origin'
      });

      // Server may return:
      // - 302 redirect if existing MARK (Location header points to /search?allow_edit_existing=1)
      // - 200 if success
      // - Other status if error

      hideLoadingOverlay();

      // Check if server redirected (existing MARK / reclassification required)
      if (res.redirected || res.status === 302 || res.url.includes('allow_edit_existing')) {
        // Show the existing banner instead of error
        const mark = receipt.mark || receipt.MARK || '?';
        showFlash('Το MARK ' + mark + ' υπάρχει ήδη στο Excel', 'warning', 4000);
        showExistingBanner(mark);
        return false;
      }

      // When fetch followed redirect, backend returns full HTML (search page).
      // Detect a rendered yellow banner in that HTML and preserve reclassification flow.
      const contentType = (res.headers.get('content-type') || '').toLowerCase();
      if (contentType.includes('text/html')) {
        const html = await res.text().catch(() => '');
        if (html && html.indexOf('id="existingBanner"') !== -1) {
          let markFromHtml = (receipt && (receipt.mark || receipt.MARK)) || '';
          try {
            const parsed = new DOMParser().parseFromString(html, 'text/html');
            const strong = parsed.querySelector('#existingBanner strong');
            if (strong && strong.textContent) markFromHtml = strong.textContent.trim();
          } catch(_) {}
          showFlash('Το MARK ' + (markFromHtml || '?') + ' υπάρχει ήδη στο Excel', 'warning', 4000);
          showExistingBanner(markFromHtml || '?');
          return false;
        }
      }

      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(`Save failed: ${res.status}`);
      }

      // Success - update table optimistically
      const urlInput = $id('scrapeUrlInput');
      if (urlInput) urlInput.value = '';

      const markInput = $id('markInput');
      if (markInput) markInput.value = '';

      showFlash('✓ Αποθηκεύτηκε η απόδειξη', 'success', 2500);
      hideModal();

      // Refresh list-inner table without full page reload.
      try {
        const reloadFn = window.partiallyReloadInvoiceTable;
        if (typeof reloadFn === 'function') {
          const ok = await reloadFn();
          if (!ok) {
            showFlash('Η εγγραφή αποθηκεύτηκε, αλλά δεν μπόρεσε να γίνει μερική ανανέωση του πίνακα.', 'warning', 3500);
          }
        } else {
          showFlash('Η εγγραφή αποθηκεύτηκε. Απαιτείται χειροκίνητη ανανέωση πίνακα.', 'warning', 3500);
        }
      } catch(_) {
        showFlash('Η εγγραφή αποθηκεύτηκε. Απαιτείται χειροκίνητη ανανέωση πίνακα.', 'warning', 3500);
      }

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
