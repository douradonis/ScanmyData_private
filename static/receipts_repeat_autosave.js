
/*! rc_receipts_autoconfirm_patch.js
 * Drop-in patch for auto-confirming RECEIPTS when "repeat" is ON.
 * Include this BEFORE receipts_repeat_autosave.js in search.html.
 */
(function(){
  const LS = window.localStorage, SS = window.sessionStorage;

  const $ = (sel) => document.querySelector(sel);
  const byId = (id) => document.getElementById(id);

  function getBoolLS(key, def=false){
    try{
      const v = LS.getItem(key);
      if(v === "1" || v === "true") return true;
      if(v === "0" || v === "false") return false;
      return def;
    }catch(e){ return def; }
  }
  function setBoolLS(key, val){
    try{ LS.setItem(key, val ? "1" : "0"); }catch(_){}
  }

  function repOn(){
    try{
      const sw = byId("repeatEntrySwitch");
      if (sw) return !!sw.checked;
    }catch(_){}
    return getBoolLS("REPEAT:enabled", false);
  }
  function recOn(){
    try{
      const sw = byId("useReceiptsSwitch");
      if (sw) return !!sw.checked;
    }catch(_){}
    return getBoolLS("UI:useReceipts", false);
  }
  function setRep(on){
    setBoolLS("REPEAT:enabled", !!on);
    try{
      document.body.dataset.repeatEnabled = on ? "1" : "0";
      window.REPEAT_ENABLED = !!on;
    }catch(_){}
  }
  function setRec(on){
    setBoolLS("UI:useReceipts", !!on);
    try{ window.USE_RECEIPTS = !!on; }catch(_){}
  }

  function persistFlash(message, type){
    try{
      sessionStorage.setItem('RC:lastFlash', JSON.stringify({ message: String(message||'').trim() || message, type: type || 'info', ts: Date.now() }));
    }catch(_){}
  }

  // Initial bridge (keep in sync with server-rendered switches)
  setRep(repOn());
  setRec(recOn());

  // Keep in sync on switch changes
  const useSw = byId("useReceiptsSwitch");
  if (useSw){
    useSw.addEventListener("change", () => setRec(useSw.checked));
  }
  const repSw = byId("repeatEntrySwitch");
  if (repSw){
    repSw.addEventListener("change", () => setRep(repSw.checked));
  }

  // Mark search form hook: mark that we want auto-confirm on this cycle
  const markForm = byId("markSearchForm");
  if (markForm){
    markForm.addEventListener("submit", () => {
      setRec(useSw ? useSw.checked : recOn());
      setRep(repSw ? repSw.checked : repOn());
      if (recOn() && repOn()){
        SS.setItem("RC:autoConfirm", "receipts");
      } else {
        SS.removeItem("RC:autoConfirm");
      }
    }, {capture:true});
  }

  // Helper: meaningful summary?
  function meaningful(obj){
    if (!obj || typeof obj !== "object") return false;
    const m = String(obj.mark || obj.MARK || "").trim();
    if (m && /^\d{15}$/.test(m)) return true;
    if (obj.totalValue && String(obj.totalValue).trim()) return true;
    const lines = obj.lines || [];
    for (const l of lines){
      if (!l) continue;
      if (String((l.description||"")).trim() || String((l.amount||"")).trim()) return true;
    }
    return false;
  }

  async function confirmReceipt(summary){
    try{
      const res = await fetch("/api/confirm_receipt", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ summary })
      });
      const j = await res.json().catch(()=>({}));
      console.log("[RC-AUTO] confirm_receipt resp", j);
      const ok = !!(res.ok && (!j || j.ok !== false));
      return { ok, data: j };
    }catch(e){
      console.warn("[RC-AUTO] confirm_receipt failed", e);
      return { ok: false, error: e };
    }
  }

  async function refreshTableAfterAutoSave(){
    try {
      if (typeof window.partiallyReloadInvoiceTable === 'function') {
        const ok = await window.partiallyReloadInvoiceTable();
        if (ok) return true;
      }
    } catch(_) {}
    return false;
  }

  // Watch summary input and auto-confirm when ready
  const inp = byId("summaryJsonInput");
  if (!inp) return;

  let lastMarkProcessed = null;

  function tryAuto(){
    if (!(recOn() && repOn())) return;
    let obj;
    try{
      obj = JSON.parse(inp.value || "{}");
    }catch(e){
      return;
    }
    if (!meaningful(obj)) return;

    const mark = String(obj.mark || obj.MARK || "").trim();
    if (lastMarkProcessed && mark && lastMarkProcessed === mark) return;
    lastMarkProcessed = mark || ("r-"+Date.now());

    // One-shot
    SS.removeItem("RC:autoConfirm");

    // Normalize receipt lines/category
    if (!obj.lines || obj.lines.length === 0){
      const v = obj.totalValue || obj.total_amount || obj.totalNetValue || "";
      obj.lines = [{
        id: "r0",
        description: "",
        amount: String(v),
        vat: "",
        category: "αποδειξακια",
        vat_category: ""
      }];
    } else {
      obj.lines = obj.lines.map((l, i) => ({
        id: (l && l.id) || ("r"+i),
        description: (l && l.description) || "",
        amount: (l && l.amount) || "",
        vat: (l && l.vat) || "",
        category: (l && l.category) || "αποδειξακια",
        vat_category: (l && l.vat_category) || (l && l.vatCategory) || ""
      }));
    }
    obj.type = "ΑΠΟΔΕΙΞΗ";
    obj.type_name = "ΑΠΟΔΕΙΞΗ";
    obj.is_receipt = true;

    console.log("[RC-AUTO] auto-confirming receipt", obj);
    confirmReceipt(obj)
      .then(result => {
        if(result && result.ok){
          persistFlash('Αποθηκεύτηκε η απόδειξη (repeat).', 'success');
          try {
            const alreadyMsg = String((result.data && (result.data.already_classified_message || result.data.warning_message)) || '').trim();
            if (alreadyMsg && typeof window.showFlash === 'function') {
              window.showFlash(alreadyMsg, 'warning', 4500);
            }
          } catch(_) {}
        } else if(result) {
          const err = (result.data && result.data.error) ? String(result.data.error) : (result.error ? String(result.error) : '');
          const msg = err ? `Σφάλμα αυτόματης αποθήκευσης: ${err}` : 'Σφάλμα αυτόματης αποθήκευσης αποδείξεων.';
          persistFlash(msg, 'error');
        }
      })
      .finally(async () => {
        const reloaded = await refreshTableAfterAutoSave();
        if (!reloaded) {
          try {
            if (typeof window.showFlash === 'function') {
              window.showFlash('Η αποθήκευση ολοκληρώθηκε, αλλά η μερική ανανέωση πίνακα απέτυχε.', 'warning', 3500);
            }
          } catch(_) {}
        }
      });
  }

  // If submit flagged auto-confirm, poll briefly until scraper fills the summary
  if (SS.getItem("RC:autoConfirm") === "receipts"){
    let n = 0;
    const timer = setInterval(() => {
      n++; tryAuto();
      if (n > 60) clearInterval(timer); // ~12s
    }, 200);
  }

  // Also react to any late updates
  inp.addEventListener("input", tryAuto);
})();
