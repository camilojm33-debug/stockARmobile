(() => {
  function initInvoiceAI() {
    const root = document.getElementById('invoice-ai-root');
    if (!root || root.dataset.invoiceAiInitialized === '1') return;
    root.dataset.invoiceAiInitialized = '1';

    const $ = (id) => document.getElementById(id);
    const fileInput = $('invoice-file-input');
    const cameraInput = $('invoice-camera-input');
    const dropzone = $('invoice-dropzone');
    const selected = $('invoice-selected');
    const errorBox = $('invoice-error');
    const processBtn = $('invoice-process');
    const clearBtn = $('invoice-clear');
    const progress = $('invoice-progress');
    const progressBar = $('invoice-progress-bar');

    if (!fileInput || !cameraInput || !dropzone || !selected || !processBtn || !clearBtn) return;

    const csrfToken = root.dataset.csrf || '';
    const uploadUrl = root.dataset.uploadUrl;
    const previewUrl = root.dataset.previewUrl;
    const processUrl = root.dataset.processUrl;
    const resolveUrl = root.dataset.resolveUrl;
    const confirmUrl = root.dataset.confirmUrl;

    let currentFile = null;
    let currentObjectUrl = null;
    let currentUploadId = null;
    let currentPreview = null;
    let pickerLine = null;
    let currentCandidates = [];
    let scannerStream = null;
    let scannerTimer = null;
    let scannerBusy = false;

    const showError = (message) => {
      if (!errorBox) return;
      errorBox.textContent = message || 'No se pudo procesar la factura.';
      errorBox.classList.remove('d-none');
    };

    const clearError = () => {
      if (!errorBox) return;
      errorBox.textContent = '';
      errorBox.classList.add('d-none');
    };

    const esc = (value) => {
      const div = document.createElement('div');
      div.textContent = value == null ? '' : String(value);
      return div.innerHTML;
    };

    const releasePreviewUrl = () => {
      if (currentObjectUrl) {
        URL.revokeObjectURL(currentObjectUrl);
        currentObjectUrl = null;
      }
    };

    async function jsonResponse(response) {
      const type = response.headers.get('content-type') || '';
      if (!type.includes('application/json')) throw new Error('El servidor no devolvió una respuesta válida.');
      const data = await response.json();
      if (!response.ok || !data.success) throw new Error(data.error || 'No se pudo procesar la factura.');
      return data;
    }

    function renderSelectedFile(file) {
      releasePreviewUrl();
      const name = String(file.name || 'Factura');
      const sizeKb = Math.max(1, Math.round(file.size / 1024));
      const lower = name.toLowerCase();
      const isImage = String(file.type || '').toLowerCase().startsWith('image/') || ['.jpg', '.jpeg', '.png', '.webp'].some((ext) => lower.endsWith(ext));
      if (isImage) {
        currentObjectUrl = URL.createObjectURL(file);
        selected.innerHTML = '<div class="d-flex align-items-center gap-3 flex-wrap"><img src="' + currentObjectUrl + '" alt="Vista previa de la factura" style="width:96px;height:96px;object-fit:cover;border-radius:12px;border:1px solid var(--app-line);background:#f8fafc"><div><div class="fw-semibold">' + esc(name) + '</div><div class="small muted">' + sizeKb + ' KB · Imagen seleccionada correctamente</div><div class="small text-success mt-1"><i class="bi bi-check-circle me-1"></i>Lista para procesar con IA</div></div></div>';
        return;
      }
      selected.innerHTML = '<div class="d-flex align-items-center gap-3 flex-wrap"><div style="width:64px;height:64px;display:grid;place-items:center;border-radius:12px;background:#eef4ff;color:#2563eb;font-size:1.5rem"><i class="bi bi-file-earmark-pdf"></i></div><div><div class="fw-semibold">' + esc(name) + '</div><div class="small muted">' + sizeKb + ' KB · PDF seleccionado correctamente</div><div class="small text-success mt-1"><i class="bi bi-check-circle me-1"></i>Listo para procesar con IA</div></div></div>';
    }

    function selectFile(file) {
      clearError();
      releasePreviewUrl();
      currentFile = file || null;
      if (!file) {
        selected.textContent = 'Todavía no seleccionaste una factura.';
        processBtn.disabled = true;
        clearBtn.disabled = true;
        return;
      }
      const name = String(file.name || '');
      const ext = name.includes('.') ? name.slice(name.lastIndexOf('.')).toLowerCase() : '';
      const allowed = ['.pdf', '.jpg', '.jpeg', '.png', '.webp'];
      if (!allowed.includes(ext)) {
        currentFile = null;
        processBtn.disabled = true;
        clearBtn.disabled = true;
        showError('Formato no compatible. Usá PDF, JPG, JPEG, PNG o WEBP.');
        return;
      }
      if (!file.size || file.size > 10 * 1024 * 1024) {
        currentFile = null;
        processBtn.disabled = true;
        clearBtn.disabled = true;
        showError('El archivo debe pesar entre 1 byte y 10 MB.');
        return;
      }
      renderSelectedFile(file);
      processBtn.disabled = false;
      clearBtn.disabled = false;
    }

    function clearFile() {
      releasePreviewUrl();
      currentFile = null;
      fileInput.value = '';
      cameraInput.value = '';
      selected.textContent = 'Todavía no seleccionaste una factura.';
      processBtn.disabled = true;
      clearBtn.disabled = true;
      clearError();
    }

    function postOptions(body) {
      const options = { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest' } };
      if (body !== undefined) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
      }
      return options;
    }

    function setProgress(value, visible = true) {
      if (!progress || !progressBar) return;
      progress.classList.toggle('d-none', !visible);
      progressBar.style.width = String(value) + '%';
    }

    async function uploadAndProcess() {
      clearError();
      if (!currentFile) return showError('Elegí una factura o sacale una foto primero.');
      processBtn.disabled = true;
      clearBtn.disabled = true;
      setProgress(20, true);
      try {
        const form = new FormData();
        form.append('message', 'Cargué una factura de proveedor para procesarla con IA.');
        form.append('agent', 'asistente');
        form.append('invoice_file', currentFile, currentFile.name);
        const uploaded = await jsonResponse(await fetch(uploadUrl, { method: 'POST', body: form, credentials: 'same-origin', headers: { 'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest' } }));
        currentUploadId = uploaded.document_id;
        setProgress(60, true);
        const processed = await jsonResponse(await fetch(processUrl.replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId)), postOptions()));
        setProgress(100, true);
        renderReview(processed.preview || {}, currentUploadId, currentFile.name);
        setTimeout(() => setProgress(0, false), 500);
      } catch (error) {
        showError(error.message);
        setProgress(0, false);
      } finally {
        processBtn.disabled = !currentFile;
        clearBtn.disabled = !currentFile;
      }
    }

    async function saveSupplierDecision(payload) {
      const data = await jsonResponse(await fetch(resolveUrl.replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId)), postOptions(payload)));
      currentPreview = data.preview || {};
      renderReview(currentPreview, currentUploadId, 'Factura');
    }

    function renderSupplier(preview) {
      const target = $('review-supplier');
      if (!target) return;
      const supplier = preview.supplier || {};
      const match = preview.supplier_match || {};
      const status = match.status || 'PENDIENTE';
      const name = match.name || supplier.name || '';
      let html = '<div class="d-flex flex-wrap justify-content-between gap-3"><div><div class="small muted">Proveedor detectado</div><strong>' + esc(name || 'Sin reconocer') + '</strong><div class="small muted mt-1">' + esc(status.replaceAll('_', ' ')) + '</div></div>';
      if (status === 'MATCH_EXACTO' || status === 'NUEVO_PROVEEDOR_CONFIRMADO') {
        html += '<div class="text-end"><span class="badge text-bg-success">Proveedor listo</span></div></div>';
        target.innerHTML = html;
        return;
      }
      const candidates = Array.isArray(match.candidates) ? match.candidates : [];
      html += '<div class="d-flex flex-column gap-2" style="min-width:min(100%,520px)">';
      if (candidates.length) {
        html += '<div class="small fw-semibold">Usar proveedor existente</div><div class="d-flex flex-wrap gap-2">' + candidates.map((item) => '<button type="button" class="btn btn-sm btn-outline-primary supplier-use" data-id="' + esc(item.id) + '">' + esc(item.name) + '</button>').join('') + '</div>';
      }
      if (name) html += '<button type="button" class="btn btn-sm btn-primary supplier-create"><i class="bi bi-plus-circle me-1"></i>Crear proveedor "' + esc(name) + '"</button>';
      html += '</div></div>';
      target.innerHTML = html;
      target.querySelectorAll('.supplier-use').forEach((button) => button.addEventListener('click', async () => {
        try { clearError(); await saveSupplierDecision({ target: 'supplier', decision: 'use_existing', supplier_id: button.dataset.id }); } catch (error) { showError(error.message); }
      }));
      target.querySelector('.supplier-create')?.addEventListener('click', async () => {
        try { clearError(); await saveSupplierDecision({ target: 'supplier', decision: 'create_new' }); } catch (error) { showError(error.message); }
      });
    }

    function renderRows(preview, uploadId) {
      const target = $('review-items');
      const rows = preview.matches || [];
      if (!target) return;
      target.innerHTML = rows.map((line) => {
        const status = line.matching_status;
        const automatic = status === 'MATCH_EXACTO';
        const isNew = status === 'NUEVO_PRODUCTO';
        const action = '<button type="button" class="btn btn-sm ' + (automatic ? 'btn-outline-secondary' : 'btn-outline-primary') + ' picker-open" data-line="' + esc(line.line_number) + '">' + (automatic ? 'Cambiar' : 'Elegir / escanear') + '</button>';
        const product = line.product_name ? '<div class="small fw-semibold">' + esc(line.product_name) + '</div><div class="small muted">Código: ' + esc(line.product_code || 'sin código') + '</div>' : '<div class="small muted">Sin vincular' + (isNew ? ' · se crearía al aplicar' : '') + '</div>';
        const confidenceClass = line.confidence_level === 'ALTA' ? 'text-bg-success' : line.confidence_level === 'MEDIA' ? 'text-bg-warning text-dark' : 'text-bg-secondary';
        return '<tr><td><strong>' + esc(line.description || 'Sin descripción') + '</strong><div class="small muted">Código factura: ' + esc(line.code || line.barcode || 'sin código') + '</div></td><td>' + product + '</td><td><span class="badge ' + confidenceClass + '">' + esc(line.confidence_level || 'BAJA') + (line.proposal_score ? ' · ' + Math.round(line.proposal_score * 100) + '%' : '') + '</span></td><td>' + esc(line.quantity) + '</td><td>' + esc(line.unit_cost) + '</td><td class="text-end">' + action + '</td></tr>';
      }).join('');
      target.querySelectorAll('.picker-open').forEach((button) => button.addEventListener('click', () => openPicker(uploadId, button.dataset.line)));
    }

    function renderReview(preview, uploadId, name) {
      currentUploadId = uploadId;
      currentPreview = preview;
      const box = $('invoice-review');
      if (!box) return;
      const invoice = preview.invoice || preview || {};
      const supplier = invoice.supplier || preview.supplier || {};
      const supplierMatch = preview.supplier_match || invoice.supplier_match || {};
      const status = preview.status || invoice.status || 'REQUIERE_REVISION';
      box.classList.remove('d-none');
      $('review-title').textContent = invoice.invoice_number ? 'Factura ' + invoice.invoice_number : (name || 'Factura');
      $('review-status').textContent = String(status).replaceAll('_', ' ');
      $('review-kpis').innerHTML = [['Proveedor', supplier.name || supplierMatch.name || 'Sin reconocer'], ['Fecha', invoice.issue_date || '—'], ['Total', invoice.total ?? '—'], ['Líneas', (preview.matches || invoice.matches || []).length]].map((item) => '<div class="col-6 col-lg-3"><div class="invoice-kpi"><div class="small muted">' + esc(item[0]) + '</div><strong>' + esc(item[1]) + '</strong></div></div>').join('');
      renderSupplier(preview);
      renderRows(preview, uploadId);
      const confirm = $('review-confirm');
      if (confirm) confirm.disabled = status !== 'LISTA_PARA_CONFIRMAR';
      box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function openPicker(uploadId, lineNumber) {
      const line = (currentPreview && currentPreview.matches || []).find((item) => String(item.line_number) === String(lineNumber));
      if (!line) return;
      pickerLine = lineNumber;
      currentCandidates = line.candidate_products || [];
      $('picker-title').textContent = line.description || 'Producto';
      $('picker-query').value = '';
      $('picker-code').value = '';
      $('picker-code').placeholder = 'Código / SKU';
      const hint = $('picker-code-hint');
      if (hint) hint.textContent = 'Podés escribir el código, usar la cámara o conectar un lector láser/USB/Bluetooth.';
      renderCandidates(currentCandidates);
      $('invoice-picker').classList.add('show');
      $('picker-code')?.focus();
    }

    function renderCandidates(candidates) {
      const list = $('picker-list');
      if (!list) return;
      list.innerHTML = candidates.length ? candidates.map((item) => '<div class="candidate"><div><strong>' + esc(item.name) + '</strong><div class="small muted">' + esc(item.code || 'Sin código') + (item.category ? ' · ' + esc(item.category) : '') + '</div></div><button type="button" class="btn btn-sm btn-primary picker-use" data-name="' + esc(item.name) + '">Usar</button></div>').join('') : '<div class="small muted">No encontramos sugerencias. Escribí el código exacto, usá la cámara o conectá el lector.</div>';
      list.querySelectorAll('.picker-use').forEach((button) => button.addEventListener('click', async () => {
        try {
          clearError();
          const data = await jsonResponse(await fetch(resolveUrl.replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId)), postOptions({ line_number: pickerLine, description: button.dataset.name })));
          $('invoice-picker').classList.remove('show');
          currentPreview = data.preview || {};
          renderReview(currentPreview, currentUploadId, 'Factura');
        } catch (error) { showError(error.message); }
      }));
    }

    async function assignCode(code) {
      const value = String(code || '').trim();
      if (!value) throw new Error('Escribí o escaneá el código/SKU del producto.');
      const data = await jsonResponse(await fetch(resolveUrl.replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId)), postOptions({ line_number: pickerLine, code: value })));
      $('invoice-picker').classList.remove('show');
      currentPreview = data.preview || {};
      renderReview(currentPreview, currentUploadId, 'Factura');
    }

    async function useTypedCode() {
      const input = $('picker-code');
      const code = input ? input.value.trim() : '';
      try {
        clearError();
        await assignCode(code);
      } catch (error) { showError(error.message); }
    }

    function stopScanner() {
      if (scannerTimer) {
        clearTimeout(scannerTimer);
        scannerTimer = null;
      }
      if (scannerStream) {
        scannerStream.getTracks().forEach((track) => track.stop());
        scannerStream = null;
      }
      const video = $('scanner-video');
      if (video) {
        video.pause();
        video.srcObject = null;
      }
      scannerBusy = false;
      const overlay = $('invoice-scanner');
      if (overlay) {
        overlay.classList.remove('show');
        overlay.setAttribute('aria-hidden', 'true');
      }
    }

    async function openScanner() {
      clearError();
      const overlay = $('invoice-scanner');
      const video = $('scanner-video');
      const status = $('scanner-status');
      if (!overlay || !video || !status) return;
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
        showError('La cámara necesita HTTPS y permisos del navegador. Podés usar el lector láser o escribir el código.');
        return;
      }
      overlay.classList.add('show');
      overlay.setAttribute('aria-hidden', 'false');
      status.textContent = 'Solicitando acceso a la cámara…';
      try {
        scannerStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
        video.srcObject = scannerStream;
        await video.play();
      } catch (error) {
        status.textContent = 'No se pudo abrir la cámara. Revisá los permisos del navegador.';
        showError('No se pudo abrir la cámara. Revisá el permiso de cámara y probá nuevamente.');
        return;
      }

      if (!('BarcodeDetector' in window)) {
        status.textContent = 'Tu navegador muestra la cámara, pero no tiene escaneo de códigos integrado. Usá el lector láser o el código manual.';
        return;
      }

      try {
        let detector;
        if (typeof window.BarcodeDetector.getSupportedFormats === 'function') {
          const supported = await window.BarcodeDetector.getSupportedFormats();
          const wanted = ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128', 'code_39', 'itf', 'codabar'];
          const formats = wanted.filter((format) => supported.includes(format));
          detector = formats.length ? new window.BarcodeDetector({ formats }) : new window.BarcodeDetector();
        } else {
          detector = new window.BarcodeDetector();
        }
        status.textContent = 'Cámara activa. Centrá el código dentro del recuadro…';
        scanWithDetector(detector, video, status);
      } catch (error) {
        status.textContent = 'No se pudo iniciar el lector de códigos de este navegador. Usá el lector láser o escribí el código.';
      }
    }

    async function scanWithDetector(detector, video, status) {
      if (!scannerStream || scannerBusy || !video.srcObject) return;
      try {
        if (video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) {
          const results = await detector.detect(video);
          const found = (results || []).find((item) => String(item.rawValue || '').trim());
          if (found) {
            scannerBusy = true;
            const value = String(found.rawValue).trim();
            status.textContent = 'Código detectado: ' + value;
            stopScanner();
            try {
              clearError();
              await assignCode(value);
            } catch (error) {
              showError(error.message);
            }
            return;
          }
        }
      } catch (error) {
        // Un frame no decodificable es normal; seguimos escaneando.
      }
      scannerTimer = setTimeout(() => scanWithDetector(detector, video, status), 180);
    }

    function focusLaserReader() {
      const input = $('picker-code');
      if (!input) return;
      clearError();
      input.value = '';
      input.placeholder = 'Esperando lector láser…';
      input.focus();
      const hint = $('picker-code-hint');
      if (hint) hint.textContent = 'Lector activo: escaneá el código de barras con el dispositivo USB/Bluetooth. Al terminar, el lector suele enviar Enter y StockAR lo asigna automáticamente.';
    }

    async function openInvoice(uploadId) {
      try {
        const data = await jsonResponse(await fetch(previewUrl.replace('__UPLOAD_ID__', encodeURIComponent(uploadId)), { credentials: 'same-origin' }));
        renderReview(data.preview || {}, uploadId, data.original_name || 'Factura');
      } catch (error) { showError(error.message); }
    }

    fileInput.addEventListener('change', () => selectFile(fileInput.files && fileInput.files[0]));
    cameraInput.addEventListener('change', () => selectFile(cameraInput.files && cameraInput.files[0]));
    processBtn.addEventListener('click', uploadAndProcess);
    clearBtn.addEventListener('click', clearFile);
    document.querySelectorAll('.invoice-open').forEach((button) => button.addEventListener('click', () => openInvoice(button.dataset.uploadId)));
    $('picker-close')?.addEventListener('click', () => $('invoice-picker').classList.remove('show'));
    $('picker-code-use')?.addEventListener('click', useTypedCode);
    $('picker-code')?.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); useTypedCode(); } });
    $('picker-camera-open')?.addEventListener('click', openScanner);
    $('picker-laser-focus')?.addEventListener('click', focusLaserReader);
    $('scanner-close')?.addEventListener('click', stopScanner);
    $('scanner-stop')?.addEventListener('click', stopScanner);
    $('picker-query')?.addEventListener('input', () => {
      const query = $('picker-query').value.trim().toLowerCase();
      renderCandidates(currentCandidates.filter((item) => String(item.name || '').toLowerCase().includes(query) || String(item.code || '').toLowerCase().includes(query)));
    });
    $('review-confirm')?.addEventListener('click', async () => {
      const button = $('review-confirm');
      button.disabled = true;
      clearError();
      try {
        const data = await jsonResponse(await fetch(confirmUrl.replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId)), postOptions()));
        const alert = $('review-alert');
        alert.className = 'alert alert-success';
        alert.textContent = data.duplicate ? 'Esta factura ya estaba aplicada anteriormente.' : 'Factura confirmada. La compra, el proveedor y el stock fueron actualizados.';
        alert.classList.remove('d-none');
        setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        showError(error.message);
        button.disabled = false;
      }
    });
    dropzone.addEventListener('dragover', (event) => { event.preventDefault(); dropzone.classList.add('dragover'); });
    dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
    dropzone.addEventListener('drop', (event) => { event.preventDefault(); dropzone.classList.remove('dragover'); const file = event.dataTransfer && event.dataTransfer.files ? event.dataTransfer.files[0] : null; selectFile(file); });
    window.addEventListener('pagehide', stopScanner);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initInvoiceAI, { once: true });
  else initInvoiceAI();
})();
