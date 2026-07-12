// Utilidades seguras para pantallas de edicion de venta.
function recalculateSaleTotals() {
  let subtotal = 0;
  document.querySelectorAll('[data-line-total]').forEach(function(cell) {
    subtotal += parseFloat(cell.dataset.lineTotal || cell.textContent.replace(/[^0-9.-]/g, '')) || 0;
  });
  const tax = subtotal * 0.21;
  const total = subtotal + tax;
  const subtotalInput = document.getElementById('subtotal-input');
  const taxInput = document.getElementById('tax-input');
  const totalInput = document.getElementById('total-input');
  if (subtotalInput) subtotalInput.value = subtotal.toFixed(2);
  if (taxInput) taxInput.value = tax.toFixed(2);
  if (totalInput) totalInput.value = total.toFixed(2);
}
