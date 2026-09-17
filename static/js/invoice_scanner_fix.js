(() => {
  function initInvoiceScannerFix() {
    const root = document.getElementById('invoice-ai-root');
    if (!root || root.dataset.invoiceScannerFixInitialized === '1') return;
    root.dataset.invoiceScannerFixInitialized = '1';

    const codeInput = document.getElementById('picker-code');
    const codeButton = document.getElementById('picker-code-use');
    const cameraButton = document.getElementById('picker-camera-open');
    if (!codeInput || !codeButton || !cameraButton) return;

    let activeUploadId = '';
    let activeLine = '';

    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
      try {
        const requestUrl = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
        const match = String(requestUrl).match(/\/ai-agent\/invoices\/([^/]+)\/(?:process|resolve|resolve-code)/);
        if (match) activeUploadId = decodeURIComponent(match[1]);
      } catch (_) {}
      return nativeFetch(...args);
    };

    const feedback = document.createElement('div');
    feedback.id = 'invoice-scanner-fix-feedback';
    feedback.className = 'small mt-2 muted';
    cameraButton.parentElement?.insertAdjacentElement('afterend', feedback);

    function setFeedback(message, type = 'muted') {
      feedback.className = `small mt-2 ${type === 'error' ? 'text-danger' : type === 'success' ? 'text-success' : 'muted'}`;
      feedback.textContent = message || '';
    }

    function resolveCodeEndpoint() {
      return String(root.dataset.resolveUrl || '').replace(/\/resolve$/, '/resolve-code');
    }

    function rememberCurrentInvoice() {
      if (activeUploadId) sessionStorage.setItem('stockar_invoice_reopen', activeUploadId);
    }

    async function assignCode(value) {
      const code = String(value || '').trim();
      if (!code) {
        setFeedback('No se recibió ningún código.', 'error');
        return;
      }
      if (!activeUploadId || !activeLine) {
        setFeedback('No pude identificar la factura o línea activa. Cerrá “Elegir producto” y volvé a abrirla.', 'error');
        return;
      }

      const endpointTemplate = resolveCodeEndpoint();
      if (!endpointTemplate) {
        setFeedback('No está disponible el servicio de asignación de códigos.', 'error');
        return;
      }

      const endpoint = endpointTemplate.replace('__UPLOAD_ID__', encodeURIComponent(activeUploadId));
      const csrfToken = root.dataset.csrf || '';
      codeButton.disabled = true;
      cameraButton.disabled = true;
      codeInput.disabled = true;
      setFeedback(`Asignando código ${code}…`);

      try {
        const response = await nativeFetch(endpoint, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken,
            'X-Requested-With': 'XMLHttpRequest',
          },
          body: JSON.stringify({ line_number: activeLine, code }),
        });
        const type = response.headers.get('content-type') || '';
        const data = type.includes('application/json') ? await response.json() : null;
        if (!response.ok || !data || !data.success) {
          throw new Error((data && data.error) || 'No se pudo asignar el código.');
        }

        setFeedback(`Código ${code} asignado a ${data.product?.name || 'el producto'}.`, 'success');
        rememberCurrentInvoice();
        document.getElementById('invoice-picker')?.classList.remove('show');
        window.setTimeout(() => window.location.reload(), 250);
      } catch (error) {
        setFeedback(error.message || 'No se pudo asignar el código.', 'error');
      } finally {
        codeButton.disabled = false;
        cameraButton.disabled = false;
        codeInput.disabled = false;
      }
    }

    async function openRobustCamera() {
      clearTimeout(window.__stockarScannerTimer);
      setFeedback('Abriendo cámara y preparando lector de códigos…');
      if (!window.StockArBarcodeScanner?.openScanner) {
        setFeedback('El lector de cámara todavía no está disponible. Recargá la página y probá nuevamente.', 'error');
        return;
      }

      await window.StockArBarcodeScanner.openScanner({
        onDetected: (code) => {
          codeInput.value = String(code || '').trim();
          assignCode(code);
        },
        onError: (error) => {
          setFeedback(error?.message || 'No se pudo iniciar el escáner.', 'error');
        },
        onCancel: () => {},
      });
    }

    function tryReopenInvoice() {
      const uploadId = sessionStorage.getItem('stockar_invoice_reopen');
      if (!uploadId) return;
      sessionStorage.removeItem('stockar_invoice_reopen');
      window.setTimeout(() => {
        const button = document.querySelector(`.invoice-open[data-upload-id="${CSS.escape(uploadId)}"]`);
        if (button) button.click();
      }, 350);
    }

    document.addEventListener('click', (event) => {
      const picker = event.target.closest?.('#review-items .picker-open');
      if (picker) {
        activeLine = String(picker.dataset.line || '');
      }
      const invoice = event.target.closest?.('.invoice-open[data-upload-id]');
      if (invoice) {
        activeUploadId = String(invoice.dataset.uploadId || '');
      }
    }, true);

    document.addEventListener('click', (event) => {
      if (event.target.closest?.('#picker-camera-open')) {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        openRobustCamera();
      }
      if (event.target.closest?.('#picker-code-use')) {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation();
        assignCode(codeInput.value);
      }
    }, true);

    codeInput.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      event.stopPropagation();
      assignCode(codeInput.value);
    }, true);

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', tryReopenInvoice, { once: true });
    } else {
      tryReopenInvoice();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initInvoiceScannerFix, { once: true });
  } else {
    initInvoiceScannerFix();
  }
})();
