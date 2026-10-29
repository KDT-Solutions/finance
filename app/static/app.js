// Confirmation for destructive forms (inline handlers are blocked by the CSP).
document.addEventListener("submit", function (ev) {
  var f = ev.target;
  if (f && f.dataset && f.dataset.confirm && !window.confirm(f.dataset.confirm)) {
    ev.preventDefault();
  }
});

// Clickable table rows (data-href), links and buttons inside keep their own behaviour.
document.addEventListener("click", function (ev) {
  var row = ev.target.closest && ev.target.closest("tr[data-href]");
  if (!row || ev.target.closest("a, button, input, select, form")) return;
  window.location.href = row.dataset.href;
});

// Horizontal scroll container for wide tables on small screens (card tables excluded).
document.querySelectorAll("table:not(.cards)").forEach(function (t) {
  if (t.parentElement && t.parentElement.classList.contains("tw")) return;
  var w = document.createElement("div");
  w.className = "tw";
  t.parentNode.insertBefore(w, t);
  w.appendChild(t);
});

// Payment file just created: start the download once, then drop ?dl= so a reload does not download again.
(function () {
  var a = document.getElementById("autodownload");
  if (!a) return;
  a.click();
  try {
    var u = new URL(window.location.href);
    u.searchParams.delete("dl");
    window.history.replaceState(null, "", u.pathname + u.search);
  } catch (e) { /* old browser: a reload downloads again, harmless */ }
})();

// Gutschrift / Provision: das Konto ist ein Ertragskonto, kein Aufwandkonto. Beschriftung umschalten und bei
// leerem Feld das Provisions-Ertragskonto des Mandanten vorschlagen.
document.querySelectorAll("select[data-acc-switch]").forEach(function (sel) {
  var form = sel.form;
  if (!form) return;
  var update = function () {
    var credit = sel.value === "credit";
    form.querySelectorAll("[data-acc-label]").forEach(function (el) {
      var inHeader = el.closest("th");
      el.textContent = credit ? (inHeader ? "Ertragskonto" : "Ertragskonto (Provision)") : "Aufwandkonto";
    });
    var acc = form.querySelector("input[name='account_no']");
    if (credit && acc && !acc.value && sel.dataset.commissionAccount) {
      acc.value = sel.dataset.commissionAccount;
    }
  };
  sel.addEventListener("change", update);
});
