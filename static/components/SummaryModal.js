/**
 * SummaryModal - Main modal for displaying & editing receipt/invoice summaries
 * Features:
 * - Single/multi-line rendering (buttons vs table)
 * - MTYPE selection for invoices & receipts
 * - Category assignment persistence
 * - Dark mode support
 * - localStorage fallback
 */

import { ModalManager, SubmitGuard, FocusManager } from '../utils/modalHelpers.js';
import {
  escapeHtml,
  getLabelForCategory,
  normalizeSummary,
  buildSingleLineHTML,
  buildTableHTML,
  readCategoriesFromUI,
  persistSummaryToInput
} from '../utils/summaryHelpers.js';

export class SummaryModal extends ModalManager {
  constructor(options = {}) {
    const element = options.element || document.getElementById('summaryModal');
    super(element);

    this.options = {
      categories: options.categories || [],
      expenseCategories: options.expenseCategories || window.G_CATEGORY_DATA?.expense_categories || [],
      mtypeOptions: options.mtypeOptions || window.G_CATEGORY_DATA?.mtype_options || {},
      allowMtypeChange: options.allowMtypeChange !== false,
      showSaveButton: options.showSaveButton !== false,
      ...options
    };

    this.currentSummary = null;
    this.submitGuard = new SubmitGuard();
    this.focusManager = new FocusManager();

    this._setupDOM();
    this._wireEvents();
  }

  _setupDOM() {
    if (!this.element) {
      // Create modal if not exists
      const html = this._getModalHTML();
      document.body.insertAdjacentHTML('beforeend', html);
      this.element = document.getElementById('summaryModal');
    }
  }

  _getModalHTML() {
    return `
      <div id="summaryModal" class="modal-summary" role="dialog" aria-modal="true" aria-labelledby="summaryModalTitle">
        <div class="modal-summary-backdrop"></div>
        <div class="modal-summary-card">
          <!-- Header -->
          <div class="modal-summary-header">
            <h2 id="summaryModalTitle" class="modal-summary-title">Περίληψη</h2>
            <button class="modal-summary-close" aria-label="Κλείσιμο" type="button">
              <span aria-hidden="true">&times;</span>
            </button>
          </div>

          <!-- Info Grid -->
          <div class="modal-summary-info">
            <div class="info-row">
              <label>MARK / Αποδ/Κία:</label>
              <span id="summaryMark" class="info-value">--</span>
            </div>
            <div class="info-row">
              <label>Ημ/νία:</label>
              <span id="summaryDate" class="info-value">--</span>
            </div>
            <div class="info-row">
              <label>ΑΦΜ Εταιρείας:</label>
              <span id="summaryCompanyAfm" class="info-value">--</span>
            </div>
            <div class="info-row">
              <label>Σύνολο:</label>
              <span id="summaryTotal" class="info-value">--</span>
            </div>
            <div class="info-row">
              <label>Σύνολο ΦΠΑ:</label>
              <span id="summaryVatTotal" class="info-value">--</span>
            </div>
            <div class="info-row">
              <label>Ολικό Ποσό:</label>
              <span id="summaryGrandTotal" class="info-value">--</span>
            </div>
          </div>

          <!-- Summary Lines Container -->
          <div id="summaryLinesContainer" class="modal-summary-lines"></div>

          <!-- Invoice MTYPE (conditional) -->
          <div id="invoiceMtypeContainer" class="mtype-container" style="display: none;">
            <label for="invoiceMtypeSelect">Τύπος Τιμολογίου:</label>
            <select id="invoiceMtypeSelect" name="invoice_mtype">
              <option value="">-- επίλεξε --</option>
            </select>
          </div>

          <!-- Receipt MTYPE (conditional) -->
          <div id="receiptMtypeContainerSummary" class="mtype-container" style="display: none;">
            <label for="receiptMtypeSelectSummary">Τύπος Απόδειξης:</label>
            <select id="receiptMtypeSelectSummary" name="receipt_mtype_summary">
              <option value="">-- επίλεξε --</option>
            </select>
          </div>

          <!-- Actions -->
          <div class="modal-summary-actions">
            <button type="button" class="btn btn-secondary modal-cancel-btn">Ακύρωση</button>
            <button type="button" class="btn btn-primary modal-save-btn" id="summaryModalSaveBtn">
              Αποθήκευση
            </button>
          </div>

          <!-- Hidden form inputs for backend -->
          <input type="hidden" id="summaryDataInput" name="summary_data" value="{}">
        </div>
      </div>
    `;
  }

  _wireEvents() {
    if (!this.element) return;

    // Close button
    const closeBtn = this.element.querySelector('.modal-summary-close');
    if (closeBtn) {
      closeBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.close();
      });
    }

    const cancelBtn = this.element.querySelector('.modal-cancel-btn');
    if (cancelBtn) {
      cancelBtn.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.close();
      });
    }

    // Save button
    const saveBtn = this.element.querySelector('.modal-save-btn');
    if (saveBtn) {
      saveBtn.addEventListener('click', (e) => {
        e.preventDefault();
        this._handleSave();
      });
    }

    // Escape key close
    this.wireEscapeKey();

    // Backdrop close
    this.wireBackdropClose();
  }

  /**
   * Display modal with summary data
   */
  show(summary, options = {}) {
    const normalized = normalizeSummary(summary);
    this.currentSummary = { ...normalized };

    // Update info fields
    this.element.querySelector('#summaryMark').textContent = escapeHtml(summary.mark || '--');
    this.element.querySelector('#summaryDate').textContent = escapeHtml(summary.date || '--');
    this.element.querySelector('#summaryCompanyAfm').textContent = escapeHtml(summary.companyAfm || '--');
    this.element.querySelector('#summaryTotal').textContent = escapeHtml(String(summary.total || '--'));
    this.element.querySelector('#summaryVatTotal').textContent = escapeHtml(String(summary.vatTotal || '--'));
    this.element.querySelector('#summaryGrandTotal').textContent = escapeHtml(String(summary.grandTotal || '--'));

    // Render lines
    const linesContainer = this.element.querySelector('#summaryLinesContainer');
    if (linesContainer) {
      this._renderLines(normalized.lines || []);
    }

    // Setup MTYPE selectors
    this._setupMtypeSelectors(summary);

    // Set up callbacks
    if (options.onSave) this.onSave = options.onSave;
    if (options.onClose) this.onClose = options.onClose;

    // Persist component state immediately to the hidden inputs to avoid
    // races where other page handlers read `#summaryJsonInput` before the
    // modal has written its internal state (observed in certain fast UX
    // sequences). This is defensive and idempotent.
    try {
      persistSummaryToInput('summaryDataInput', this.currentSummary);
      const legacy = document.getElementById('summaryJsonInput');
      if (legacy) {
        const merged = Object.assign({}, JSON.parse(legacy.value || '{}') || {}, this.currentSummary || {});
        legacy.value = JSON.stringify(merged);
      }
    } catch (err) { /* best-effort only */ }

    // Open modal and focus
    this.open();
    setTimeout(() => {
      this.focusManager.init(this.element);
      const firstFocusable = this.element?.querySelector('select, input, button');
      if (firstFocusable) firstFocusable.focus();
    }, 100);
  }

  _renderLines(lines) {
    const container = this.element.querySelector('#summaryLinesContainer');
    if (!container) return;

    const summary = {
      lines: lines,
      ...this.currentSummary
    };

    // Import and use summary helpers
    const categories = this.options.expenseCategories || [];
    if (lines.length === 1) {
      container.innerHTML = '';
      container.appendChild(buildSingleLineHTML(lines[0], categories));
    } else if (lines.length > 1) {
      container.innerHTML = '';
      container.appendChild(buildTableHTML(lines, categories));
    } else {
      container.innerHTML = '<div class="summary-empty-warning">Δεν βρέθηκαν γραμμές.</div>';
    }

    // Wire up change handlers
    this._wireLineInteractions();
  }

  _wireLineInteractions() {
    const container = this.element.querySelector('#summaryLinesContainer');
    if (!container) return;

    // Wire select changes
    container.querySelectorAll('select.expense-category').forEach(sel => {
      sel.addEventListener('change', (e) => {
        const lineId = e.target.dataset.lineId || '';
        const value = e.target.value || '';
        this._updateLineCategory(lineId, value);
      });
    });

    // Wire button clicks for single-line UI
    container.querySelectorAll('.category-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const lineId = btn.dataset.lineId || '';
        const cat = btn.dataset.cat || '';
        const isActive = btn.classList.contains('active');

        // Clear all buttons in this group
        const wrapper = btn.closest('.summary-line-card');
        if (wrapper) {
          wrapper.querySelectorAll('.category-btn').forEach(b => b.classList.remove('active'));
          if (!isActive) {
            btn.classList.add('active');
            this._updateLineCategory(lineId, cat);
          } else {
            this._updateLineCategory(lineId, '');
          }
        }
      });
    });

    // Wire clear buttons
    container.querySelectorAll('.clear-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.preventDefault();
        const wrapper = btn.closest('.summary-line-card');
        if (wrapper) {
          wrapper.querySelectorAll('.category-btn').forEach(b => b.classList.remove('active'));
          const lineId = wrapper.dataset.lineId || '';
          this._updateLineCategory(lineId, '');
        }
      });
    });
  }

  _updateLineCategory(lineId, category) {
    // Update in-memory summary
    const line = this.currentSummary.lines?.find(l => (l.id || l.line_id) === lineId);
    if (line) {
      line.category = category || '';
    }
  }

  _setupMtypeSelectors(summary) {
    const invoiceContainer = this.element.querySelector('#invoiceMtypeContainer');
    const receiptContainer = this.element.querySelector('#receiptMtypeContainerSummary');
    const invoiceSelect = this.element.querySelector('#invoiceMtypeSelect');
    const receiptSelect = this.element.querySelector('#receiptMtypeSelectSummary');

    // Normalize mtype options (support both legacy array-of-{value,label} and
    // the newer object shape { invoice: [...], receipt: [...] })
    const rawMtypes = this.options.mtypeOptions || window.G_CATEGORY_DATA?.mtype_options || {};
    let invoiceOptions = [];
    let receiptOptions = [];

    if (Array.isArray(rawMtypes)) {
      // legacy: array of {value,label} (or strings) -> use for both invoice & receipt
      invoiceOptions = receiptOptions = rawMtypes;
    } else if (rawMtypes && typeof rawMtypes === 'object') {
      invoiceOptions = rawMtypes.invoice || rawMtypes.invoices || rawMtypes;
      receiptOptions = rawMtypes.receipt || rawMtypes.receipts || rawMtypes;
    }

    const docType = summary.docType || summary.type || '';
    const hasInvoiceMtype = Array.isArray(invoiceOptions) && invoiceOptions.length > 0;
    const hasReceiptMtype = Array.isArray(receiptOptions) && receiptOptions.length > 0;

    // Consider receipts mode from multiple signals: explicit summary flag, type_name hint or global UI toggle
    const receiptsModeActive = Boolean(
      summary && (summary.is_receipt === true || /αποδει/i.test(String(summary.type_name || summary.type || '')))
      || (typeof window.isReceiptsOn === 'function' && window.isReceiptsOn())
    );

    // Show/hide containers and populate selects with a tolerant parser
    if (invoiceContainer && hasInvoiceMtype && docType === 'invoice') {
      invoiceContainer.style.display = 'block';
      this._populateMtypeSelect(invoiceSelect, invoiceOptions, summary.invoiceMtype || summary.mtype || '');
    } else if (invoiceContainer) {
      invoiceContainer.style.display = 'none';
    }

    // show receipt selector when summary indicates a receipt OR the receipts UI toggle is active
    if (receiptContainer && hasReceiptMtype && (docType === 'receipt' || receiptsModeActive)) {
      receiptContainer.style.display = 'block';
      this._populateMtypeSelect(receiptSelect, receiptOptions, summary.receiptMtype || summary.mtype || '');
    } else if (receiptContainer) {
      receiptContainer.style.display = 'none';
    }
  }

  _populateMtypeSelect(select, options, currentValue) {
    if (!select || !options) return;

    select.innerHTML = '<option value="">-- επίλεξε --</option>';

    // options may be:
    // - an array of strings: ['11','12']
    // - an array of objects: [{value:'11', label:'Ταμειακή'}]
    // - an object keyed by value -> label
    const list = Array.isArray(options)
      ? options
      : (typeof options === 'object' ? Object.keys(options).map(k => ({ value: k, label: options[k] })) : []);

    list.forEach(item => {
      const val = (typeof item === 'string') ? item : (item.value || item.key || '');
      const label = (typeof item === 'string') ? item : (item.label || item.value || String(val));
      const o = document.createElement('option');
      o.value = val;
      o.textContent = label;
      if (String(val) === String(currentValue)) o.selected = true;
      select.appendChild(o);
    });

    // Keep legacy hidden input and page-level state in sync when user changes selects
    const syncToLegacy = () => {
      try {
        const selVal = select.value || '';

        // Directly merge the selected value into the legacy hidden input so
        // server-side flows that read `#summaryJsonInput` always get the latest MTYPE.
        const legacyInput = document.getElementById('summaryJsonInput');
        if (legacyInput) {
          let parsed = {};
          try { parsed = JSON.parse(legacyInput.value || '{}') || {}; } catch(_) { parsed = {}; }
          parsed.mtype = selVal || parsed.mtype || '';
          if (/receipt/i.test(select.id || '')) parsed.receipt_mtype = selVal || parsed.receipt_mtype || '';
          else parsed.invoice_mtype = selVal || parsed.invoice_mtype || '';
          legacyInput.value = JSON.stringify(parsed);
        }

        // Also update the component's in-memory summary and component-hidden input
        if (this.currentSummary) {
          if (/receipt/i.test(select.id || '')) this.currentSummary.receiptMtype = selVal || '';
          else this.currentSummary.invoiceMtype = selVal || '';
          persistSummaryToInput('summaryDataInput', this.currentSummary);
        }

        // Finally, call the global helper to keep any other legacy UI in sync.
        if (typeof window.updateSummaryFromDom === 'function') {
          try { window.updateSummaryFromDom(); } catch(e) { /* ignore */ }
        }
      } catch (err) {
        console.warn('syncToLegacy failed', err);
      }
    };

    // Attach change handler (avoid duplicate attachments)
    if (!select._rc_mtype_synced) {
      select.addEventListener('change', syncToLegacy);
      select._rc_mtype_synced = true;
    }
      // Read MTYPE selections
      const invoiceSelect = this.element.querySelector('#invoiceMtypeSelect');
      const receiptSelect = this.element.querySelector('#receiptMtypeSelectSummary');

      if (invoiceSelect && invoiceSelect.style.display !== 'none') {
        this.currentSummary.invoiceMtype = invoiceSelect.value || '';
        // also expose snake_case for legacy consumers
        this.currentSummary.invoice_mtype = this.currentSummary.invoiceMtype || '';
      }

      if (receiptSelect && receiptSelect.style.display !== 'none') {
        this.currentSummary.receiptMtype = receiptSelect.value || '';
        // also expose snake_case for legacy consumers
        this.currentSummary.receipt_mtype = this.currentSummary.receiptMtype || '';
      }

      // Persist to hidden input (include both camelCase and snake_case keys)
      persistSummaryToInput('summaryDataInput', this.currentSummary);

      // Also keep legacy hidden `summaryJsonInput` in sync so server-side /save_summary
      // receives the MTYPE when the classic form is submitted.
      try {
        const legacy = document.getElementById('summaryJsonInput');
        if (legacy) {
          const merged = Object.assign({}, JSON.parse(legacy.value || '{}') || {}, this.currentSummary || {});
          legacy.value = JSON.stringify(merged);
        }
      } catch (err) { /* ignore */ }

      // Call custom handler if provided
      if (this.onSave && typeof this.onSave === 'function') {
        this.onSave(this.currentSummary);
      }

      // If the legacy save form exists, request submit so the global
      // saveSummaryForm submit-interceptor (which performs payment/MTYPE
      // validation and shows the confirmation modal) runs as expected.
      const legacyForm = document.getElementById('saveSummaryForm');
      if (legacyForm) {
        try {
          console.debug('SummaryModal: requesting legacy form submit', this.currentSummary);
          if (typeof legacyForm.requestSubmit === 'function') {
            legacyForm.requestSubmit();
            // Do not close the modal here; the global save handler will
            // hide/close it after successful save or keep it open on cancel.
            return;
          } else {
            // Fallback: dispatch submit event so page-level interceptors handle save.
            const ev = new Event('submit', { bubbles: true, cancelable: true });
            legacyForm.dispatchEvent(ev);
            return;
          }
        } catch (err) {
          console.warn('SummaryModal: legacy form submit failed', err);
        }
      }

      // No legacy form -> close modal as before
      this.close();
    } finally {
      this.submitGuard.unlock();
    }
  }

  close() {
    this.focusManager.cleanup?.();
    if (this.onClose && typeof this.onClose === 'function') {
      this.onClose();
    }
    super.close();
  }

  /**
   * Get current summary state
   */
  getSummary() {
    return { ...this.currentSummary };
  }

  /**
   * Update summary data programmatically
   */
  updateSummary(summary) {
    this.currentSummary = normalizeSummary(summary);
  }
}

export default SummaryModal;
