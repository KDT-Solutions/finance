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
