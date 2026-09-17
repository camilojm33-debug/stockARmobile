(() => {
  function loadInvoiceFlowV2() {
    const root = document.getElementById('invoice-ai-root');
    if (!root || root.dataset.invoiceFlowV2Loader === '1') return;
    root.dataset.invoiceFlowV2Loader = '1';

    const start = () => {
      if (window.__stockarInvoiceFlowV2Loaded) return;
      const script = document.createElement('script');
      script.src = '/static/js/invoice_flow_v2.js?v=20260917-final-1';
      script.async = false;
      script.onload = () => { window.__stockarInvoiceFlowV2Loaded = true; };
      script.onerror = () => {
        const error = document.getElementById('invoice-error');
        if (error) {
          error.textContent = 'No se pudo cargar el flujo actualizado de Facturas IA. Recargá la página.';
          error.classList.remove('d-none');
        }
      };
      document.head.appendChild(script);
    };

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
      start();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', loadInvoiceFlowV2, { once: true });
  } else {
    loadInvoiceFlowV2();
  }
})();
