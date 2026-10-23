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
