(function(){
  'use strict';

  function is15(x){
    try{
      const s = String(x || '').trim();
      return /^\d{15}$/.test(s);
    }catch(_){ return false; }
  }

  function isMeaningfulSummaryObject(obj){
    try{
      if (!obj || typeof obj !== 'object') return false;
      const mark = String(obj.mark || obj.MARK || '').trim();
      if (!is15(mark)) return false;

      let lines = [];
      if (Array.isArray(obj.lines)) lines = obj.lines;
      else if (obj.lines && typeof obj.lines === 'object') {
        try { lines = Object.values(obj.lines).filter(Boolean); } catch(_) { lines = []; }
      }
      if ((!lines || !lines.length) && obj.raw && Array.isArray(obj.raw.lines)) {
        lines = obj.raw.lines;
      }
      if (lines.length === 0) return false;

      // At least one line with some info
      const hasInfo = lines.some(l => {
        if (!l) return false;
        const desc = (l.description || l.desc || '').toString().trim();
        const amt  = (l.amount || l.lineTotal || l.total || '').toString().trim();
        const id   = (l.id || l.line_id || '').toString().trim();
        return !!(desc || amt || id);
      });
      if (hasInfo) return true;

      // Or totals exist
      const tv  = (obj.totalValue || obj.total_value || obj.total_amount || '').toString().trim();
      const tnv = (obj.totalNetValue || '').toString().trim();
      const tva = (obj.totalVatAmount || '').toString().trim();
      return !!(tv || (tnv && tva));
    }catch(_){ return false; }
  }

  function coerceReceiptCategoryOnSummary(obj){
    try{
      if (!obj || typeof obj !== 'object') return obj;
      obj.is_receipt = true;
      obj.category = obj.category || 'αποδειξακια';
      if (!obj.type) obj.type = 'ΑΠΟΔΕΙΞΗ';
      if (!obj.type_name) obj.type_name = 'Απόδειξη';
      if (!Array.isArray(obj.lines)) obj.lines = [];
      obj.lines = obj.lines.map(function(l){
        if (!l) return l;
        if (!l.category || String(l.category).trim() === '') l.category = 'αποδειξακια';
        return l;
      });
      return obj;
    }catch(_){ return obj; }
  }

  function isReceiptSummaryObject(obj){
    try{
      if (!obj || typeof obj !== 'object') return false;
      if (obj.is_receipt === true) return true;
      const docType = String(obj.docType || obj.doc_type || '').trim().toLowerCase();
      if (docType.startsWith('receipt')) return true;
      if (docType.startsWith('invoice')) return false;
      const t = String(obj.type || '').trim().toLowerCase();
      const tn = String(obj.type_name || '').trim().toLowerCase();
      if (t === '8.4' || t === '8.5' || t === '11.5') return true;
      if (t.includes('receipt') || tn.includes('receipt') || tn.includes('αποδειξ') || tn.includes('λιαν')) return true;
      return false;
    }catch(_){ return false; }
  }

  function getEl(id){ return document.getElementById(id); }

  function getSummary(){
    const input = getEl('summaryJsonInput');
    if (!input) return {input:null, data:null};
    try{
      return { input, data: JSON.parse(input.value || '{}') || {} };
    }catch(_){
      return { input, data: {} };
    }
  }

  function setSummary(obj){
    const input = getEl('summaryJsonInput');
    if (!input) return;
    try{
      input.value = JSON.stringify(obj || {});
    }catch(_){
      input.value = '';
    }
  }

  document.addEventListener('DOMContentLoaded', function(){

    // 0) Clear meaningless summary on load
    (function clearIfMeaningless(){
      const s = getSummary();
      if (!s.input) return;
      if (!isMeaningfulSummaryObject(s.data)){
        s.input.value = ''; // prevents background auto-submit attempts from other code
      }
    })();

    // 1) Guard markSearchForm: block empty submissions (both programmatic and normal)
    (function guardSearchForm(){
      const form = getEl('markSearchForm');
      if (!form) return;

      const origSubmit = form.submit ? form.submit.bind(form) : null;

      // Override programmatic submit
      if (origSubmit){
        form.submit = function(){
          const mark = (getEl('markInput')?.value || '').trim();
          const url  = (getEl('scrapeUrlInput')?.value || '').trim();
          if (!mark && !url) return; // BLOCK
          return origSubmit();
        };
      }

      // Intercept normal submit
      form.addEventListener('submit', function(ev){
        const mark = (getEl('markInput')?.value || '').trim();
        const url  = (getEl('scrapeUrlInput')?.value || '').trim();
        if (!mark && !url){
          ev.preventDefault();
          ev.stopImmediatePropagation();
          return false;
        }
      }, true);
    })();

    // 2) Guard saveSummaryForm: allow only if summary is meaningful
    (function guardSaveSummaryForm(){
      const form = getEl('saveSummaryForm');
      if (!form) return;

      const origSubmit = form.submit ? form.submit.bind(form) : null;

      // Override programmatic submit
      if (origSubmit){
        form.submit = function(){
          const s = getSummary();
          if (!s.input) return; // nothing to submit anyway

          // If receipts switch is ON, coerce categories for receipts before checking meaningful
          const useReceipts = !!(getEl('useReceiptsSwitch') && getEl('useReceiptsSwitch').checked);
          let data = s.data || {};
          if (useReceipts && isReceiptSummaryObject(data)){
            data = coerceReceiptCategoryOnSummary(data);
            setSummary(data);
          }

          if (!isMeaningfulSummaryObject(data)) return; // BLOCK
          return origSubmit();
        };
      }

      // Intercept normal submit
      form.addEventListener('submit', function(ev){
        const s = getSummary();
        if (!s.input){
          ev.preventDefault();
          ev.stopImmediatePropagation();
          return false;
        }
        const useReceipts = !!(getEl('useReceiptsSwitch') && getEl('useReceiptsSwitch').checked);
        let data = s.data || {};
        if (useReceipts && isReceiptSummaryObject(data)){
          data = coerceReceiptCategoryOnSummary(data);
          setSummary(data);
        }
        if (!isMeaningfulSummaryObject(data)){
          ev.preventDefault();
          ev.stopImmediatePropagation();
          return false;
        }
      }, true);
    })();

  });
})();