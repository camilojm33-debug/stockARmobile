(() => {
  function initManualCodeFix() {
    const root = document.getElementById('invoice-ai-root');
    if (!root) return;

    const codeInput = document.getElementById('picker-code');
    const codeButton = document.getElementById('picker-code-use');
    const hint = document.getElementById('picker-code-hint');
    const cameraButton = document.getElementById('picker-camera-open');
    if (!codeInput || !codeButton || !hint) return;
    if (codeButton.dataset.manualFixInitialized === '1') return;

    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const requestUrl = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
      const match = String(requestUrl).match(/\/ai-agent\/invoices\/([^/]+)\/(?:process|resolve(?:-code)?)/);
      if (match) root.dataset.activeUploadId = decodeURIComponent(match[1]);
      return nativeFetch(...args);
    };

    const cleanButton = codeButton.cloneNode(true);
    const cleanInput = codeInput.cloneNode(true);
    cleanButton.dataset.manualFixInitialized = '1';
    codeButton.replaceWith(cleanButton);
    codeInput.replaceWith(cleanInput);

    let feedback = document.getElementById('picker-code-feedback');
    if (!feedback) {
      feedback = document.createElement('div');
      feedback.id = 'picker-code-feedback';
      feedback.className = 'small mt-2';
      hint.insertAdjacentElement('afterend', feedback);
    }

    function setFeedback(message, type = 'muted') {
      feedback.className = `small mt-2 ${type === 'error' ? 'text-danger' : type === 'success' ? 'text-success' : 'muted'}`;
      feedback.textContent = message || '';
    }

    function activeLineButton() {
      return document.querySelector('#review-items .picker-open.manual-picker-active');
    }

    function resolveCodeUrl() {
      return String(root.dataset.resolveUrl || '').replace(/\/resolve(?:-code)?$/, '/resolve-code');
    }

    async function assignCode() {
      const code = cleanInput.value.trim();
      if (!code) {
        setFeedback('Escribí o escaneá el código del producto.', 'error');
        cleanInput.focus();
        return;
      }

      const lineButton = activeLineButton();
      const uploadId = root.dataset.activeUploadId || '';
      const endpointTemplate = resolveCodeUrl();
      if (!lineButton || !uploadId || !endpointTemplate) {
        setFeedback('No pude identificar la línea o la factura activa. Cerrá y volvé a abrir “Elegir producto”.', 'error');
        return;
      }

      const endpoint = endpointTemplate.replace('__UPLOAD_ID__', encodeURIComponent(uploadId));
      const csrfToken = root.dataset.csrf || '';
      cleanButton.disabled = true;
      cleanInput.disabled = true;
      setFeedback('Buscando el código en StockAR…');

      try {
        const response = await nativeFetch(endpoint, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken,
            'X-Requested-With': 'XMLHttpRequest',
          },
          body: JSON.stringify({ line_number: lineButton.dataset.line, code }),
        });
        const type = response.headers.get('content-type') || '';
        const data = type.includes('application/json') ? await response.json() : null;
        if (!response.ok || !data || !data.success) {
          throw new Error((data && data.error) || 'No se encontró un producto con ese código.');
        }

        setFeedback(`Asignado: ${data.product?.name || code}.`, 'success');
        document.getElementById('invoice-picker')?.classList.remove('show');
        window.setTimeout(() => window.location.reload(), 300);
      } catch (error) {
        setFeedback(error.message || 'No se pudo asignar el código.', 'error');
      } finally {
        cleanButton.disabled = false;
        cleanInput.disabled = false;
      }
    }

    async function loadBarcodeScanner() {
      if (window.StockArBarcodeScanner?.openScanner) return true;
      if (window.__stockArBarcodeScannerLoader) return window.__stockArBarcodeScannerLoader;
      window.__stockArBarcodeScannerLoader = new Promise((resolve) => {
        const existing = document.querySelector('script[data-stockar-barcode-scanner="1"]');
        if (existing) {
          existing.addEventListener('load', () => resolve(Boolean(window.StockArBarcodeScanner?.openScanner)), { once: true });
          existing.addEventListener('error', () => resolve(false), { once: true });
          return;
        }
        const script = document.createElement('script');
        script.src = '/static/assets/js/barcode-scanner.js?v=20260917-camera-fix';
        script.async = true;
        script.dataset.stockarBarcodeScanner = '1';
        script.onload = () => resolve(Boolean(window.StockArBarcodeScanner?.openScanner));
        script.onerror = () => resolve(false);
        document.head.appendChild(script);
      });
      return window.__stockArBarcodeScannerLoader;
    }

    async function openCameraScanner() {
      clearHint();
      setFeedback('Preparando escáner de cámara…');
      const ready = await loadBarcodeScanner();
      if (!ready) {
        setFeedback('No se pudo cargar el lector de cámara. Podés escribir el código o usar un lector láser.', 'error');
        return;
      }
      window.StockArBarcodeScanner.openScanner({
        onDetected: async (barcode) => {
          cleanInput.value = String(barcode || '').trim();
          setFeedback(`Código detectado: ${cleanInput.value}. Asignando…`, 'success');
          await assignCode();
        },
        onError: (error) => {
          setFeedback(error?.message || 'No se pudo abrir la cámara.', 'error');
        },
        onCancel: () => {
          if (!cleanInput.value) setFeedback('Escaneo cancelado. Podés escribir el código o usar el lector láser.');
        },
      });
    }

    function clearHint() {
      const scannerFeedback = document.getElementById('picker-code-feedback');
      if (scannerFeedback) scannerFeedback.textContent = '';
    }

    cleanButton.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      assignCode();
    });

    cleanInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        event.stopPropagation();
        assignCode();
      }
    });

    document.addEventListener('click', (event) => {
      const button = event.target.closest?.('#review-items .picker-open');
      if (!button) return;
      document.querySelectorAll('#review-items .picker-open.manual-picker-active').forEach((item) => item.classList.remove('manual-picker-active'));
      button.classList.add('manual-picker-active');
    }, true);

    document.addEventListener('click', (event) => {
      const button = event.target.closest?.('.invoice-open[data-upload-id]');
      if (button) root.dataset.activeUploadId = button.dataset.uploadId || '';
    }, true);

    document.addEventListener('click', async (event) => {
      const button = event.target.closest?.('#picker-camera-open');
      if (!button) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      await openCameraScanner();
    }, true);

    hint.textContent = 'Escribí el código, escanealo con la cámara o conectá un lector láser/USB/Bluetooth. La cámara usa un lector compatible incluso en navegadores sin BarcodeDetector nativo.';

    if (cameraButton) cameraButton.setAttribute('data-stockar-camera-override', '1');
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initManualCodeFix, { once: true });
  else initManualCodeFix();
})();
