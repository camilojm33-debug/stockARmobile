(() => {
  function initManualCodeFix() {
    const root = document.getElementById('invoice-ai-root');
    if (!root) return;

    const codeInput = document.getElementById('picker-code');
    const codeButton = document.getElementById('picker-code-use');
    const hint = document.getElementById('picker-code-hint');
    if (!codeInput || !codeButton || !hint) return;
    if (codeButton.dataset.manualFixInitialized === '1') return;

    // Reemplaza los controles para evitar el listener antiguo y garantizar que
    // la asignación manual use el endpoint robusto de código exacto.
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

    const getLine = () => {
      const currentTitle = document.getElementById('picker-title');
      const buttons = document.querySelectorAll('#review-items .picker-open');
      if (!currentTitle) return null;
      // El modal solo se abre desde una línea activa; guardamos el identificador
      // en el botón que abrió el selector.
      return Array.from(buttons).find((button) => button.classList.contains('manual-picker-active'));
    };

    function setFeedback(message, type = 'muted') {
      feedback.className = `small mt-2 ${type === 'error' ? 'text-danger' : type === 'success' ? 'text-success' : 'muted'}`;
      feedback.textContent = message || '';
    }

    function replaceResolveUrl(base) {
      return String(base || '').replace(/\/resolve(?:-code)?$/, '/resolve-code');
    }

    async function assignCode() {
      const code = cleanInput.value.trim();
      if (!code) {
        setFeedback('Escribí o escaneá el código del producto.', 'error');
        cleanInput.focus();
        return;
      }

      const activeButton = getLine();
      if (!activeButton) {
        setFeedback('No pude determinar la línea de factura activa. Cerrá y volvé a abrir “Elegir producto”.', 'error');
        return;
      }

      const uploadButton = document.querySelector('.invoice-open[data-upload-id]');
      const uploadId = root.dataset.activeUploadId || (uploadButton && uploadButton.dataset.uploadId) || '';
      const lineNumber = activeButton.dataset.line;
      const baseResolveUrl = root.dataset.resolveUrl || '';
      const endpointTemplate = replaceResolveUrl(baseResolveUrl);
      if (!uploadId || !lineNumber || !endpointTemplate) {
        setFeedback('No se pudo identificar la factura o la línea.', 'error');
        return;
      }

      const endpoint = endpointTemplate.replace('__UPLOAD_ID__', encodeURIComponent(uploadId));
      const csrfToken = root.dataset.csrf || '';
      cleanButton.disabled = true;
      cleanInput.disabled = true;
      setFeedback('Buscando código en StockAR…');

      try {
        const response = await fetch(endpoint, {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': csrfToken,
            'X-Requested-With': 'XMLHttpRequest',
          },
          body: JSON.stringify({ line_number: lineNumber, code }),
        });

        const type = response.headers.get('content-type') || '';
        const data = type.includes('application/json') ? await response.json() : null;
        if (!response.ok || !data || !data.success) {
          throw new Error((data && data.error) || 'No se encontró un producto con ese código.');
        }

        setFeedback('Código asignado correctamente. Actualizando la factura…', 'success');
        const picker = document.getElementById('invoice-picker');
        picker?.classList.remove('show');

        // Recarga para que el preview y el botón Confirmar y aplicar queden
        // sincronizados con el estado persistido en backend.
        window.setTimeout(() => window.location.reload(), 300);
      } catch (error) {
        setFeedback(error.message || 'No se pudo asignar el código.', 'error');
      } finally {
        cleanButton.disabled = false;
        cleanInput.disabled = false;
      }
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

    // El botón que abre el picker queda marcado para conocer la línea activa.
    document.addEventListener('click', (event) => {
      const button = event.target.closest?.('#review-items .picker-open');
      if (!button) return;
      document.querySelectorAll('#review-items .picker-open.manual-picker-active').forEach((item) => item.classList.remove('manual-picker-active'));
      button.classList.add('manual-picker-active');
      root.dataset.activeUploadId = root.dataset.activeUploadId || '';
    }, true);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initManualCodeFix, { once: true });
  else initManualCodeFix();
})();
