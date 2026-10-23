// Confirmation for destructive forms (inline handlers are blocked by the CSP).
document.addEventListener("submit", function (ev) {
  var f = ev.target;
  if (f && f.dataset && f.dataset.confirm && !window.confirm(f.dataset.confirm)) {
    ev.preventDefault();
  }
});
