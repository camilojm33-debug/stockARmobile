// Legacy compatibility shim.
// Facturas IA now uses static/js/invoice_flow_v2.js as the single UI/controller.
(() => {
  const root = document.getElementById('invoice-ai-root');
  if (root) root.dataset.invoiceScannerV2 = '1';
})();
