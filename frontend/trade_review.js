// Stage 5.9 — TRADE REVIEW controls for the Trader View proposal card. Review state only: "View Details" opens the deterministic detail
// view (and acknowledges the proposal as VIEWED), "Dismiss" marks it DISMISSED. The only network calls are the two review-state POSTs;
// nothing here previews, submits, places or cancels anything. A proposal is a review artifact, not advice.
(function () {
  "use strict";
  const state = { busy: false, last: null };

  async function post(path) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.message || `HTTP ${r.status}`);
    return j;
  }

  function note(el, text) {
    const n = el.querySelector("[data-prop-note]") || (() => { const d = document.createElement("div"); d.className = "tv-dim"; d.setAttribute("data-prop-note", ""); el.appendChild(d); return d; })();
    n.textContent = text;
  }

  function bind(el, proposal) {
    const box = el.querySelector(".tv-prop-actions");
    if (!box || box.dataset.bound) return;
    box.dataset.bound = "1";
    const id = box.dataset.proposalId;
    const details = el.querySelector("[data-prop-details]");
    const btnDetails = box.querySelector('[data-prop="details"]');
    const btnDismiss = box.querySelector('[data-prop="dismiss"]');
    if (btnDetails) btnDetails.addEventListener("click", async () => {
      const open = details && details.classList.toggle("tv-hidden") === false;
      btnDetails.setAttribute("aria-expanded", open ? "true" : "false");
      btnDetails.textContent = open ? "Hide Details" : "View Details";
      if (open && proposal && proposal.review_state === "UNREVIEWED" && !state.busy) {
        state.busy = true;
        try { state.last = await post(`/api/proposals/${encodeURIComponent(id)}/view`); proposal.review_state = state.last.review_state; }
        catch (e) { note(el, `Could not record the review state (${e.message}).`); }
        finally { state.busy = false; }
      }
    });
    if (btnDismiss) btnDismiss.addEventListener("click", async () => {
      if (state.busy || btnDismiss.disabled) return;
      state.busy = true; btnDismiss.disabled = true;
      try {
        state.last = await post(`/api/proposals/${encodeURIComponent(id)}/dismiss`);
        if (window.TraderView && typeof window.TraderView.applyProposal === "function" && state.last.proposal) window.TraderView.applyProposal(state.last.proposal);
      } catch (e) { btnDismiss.disabled = false; note(el, `Could not dismiss (${e.message}).`); }
      finally { state.busy = false; }
    });
  }

  window.TradeReview = { bind, state };
})();
