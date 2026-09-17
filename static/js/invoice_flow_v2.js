(() => {
  const root = document.getElementById('invoice-ai-root');
  if (!root || root.dataset.invoiceFlowV2 === '1') return;
  root.dataset.invoiceFlowV2 = '1';

  const $ = (id) => document.getElementById(id);
  const csrfToken = root.dataset.csrf || '';
  const uploadUrl = root.dataset.uploadUrl || '';
  const previewUrl = root.dataset.previewUrl || '';
  const processUrl = root.dataset.processUrl || '';
  const resolveUrl = root.dataset.resolveUrl || '';
  const confirmUrl = root.dataset.confirmUrl || '';
  const directCodeUrl = () => resolveUrl.replace(/\/resolve$/, '/resolve-code');

  let currentFile = null;
  let currentUploadId = null;
  let currentPreview = null;
  let pickerLine = null;
  let currentCandidates = [];
  let previewObjectUrl = null;

  const esc = (value) => {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  };

  function errorBox(message) {
    const box = $('invoice-error');
    if (!box) return;
    box.textContent = message || 'No se pudo completar la operación.';
    box.classList.remove('d-none');
  }

  function clearError() {
    const box = $('invoice-error');
    if (!box) return;
    box.textContent = '';
    box.classList.add('d-none');
  }

  async function jsonResponse(response) {
    const contentType = response.headers.get('content-type') || '';
    let data = null;
    if (contentType.includes('application/json')) data = await response.json();
    if (!response.ok || !data || !data.success) {
      throw new Error((data && data.error) || `La operación no se pudo completar (${response.status}).`);
    }
    return data;
  }

  function postOptions(body) {
    const options = {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'X-CSRFToken': csrfToken,
        'X-Requested-With': 'XMLHttpRequest',
      },
    };
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    return options;
  }

  function setProgress(value, visible = true) {
    const progress = $('invoice-progress');
    const bar = $('invoice-progress-bar');
    if (!progress || !bar) return;
    progress.classList.toggle('d-none', !visible);
    bar.style.width = `${value}%`;
  }

  function releasePreview() {
    if (previewObjectUrl) {
      URL.revokeObjectURL(previewObjectUrl);
      previewObjectUrl = null;
    }
  }

  function selectFile(file) {
    clearError();
    releasePreview();
    currentFile = file || null;
    const selected = $('invoice-selected');
    const process = $('invoice-process');
    const clear = $('invoice-clear');
    if (!file) {
      if (selected) selected.textContent = 'Todavía no seleccionaste una factura.';
      if (process) process.disabled = true;
      if (clear) clear.disabled = true;
      return;
    }
    const filename = String(file.name || 'Factura');
    const extension = filename.includes('.') ? filename.slice(filename.lastIndexOf('.')).toLowerCase() : '';
    if (!['.pdf', '.jpg', '.jpeg', '.png', '.webp'].includes(extension)) {
      currentFile = null;
      if (process) process.disabled = true;
      if (clear) clear.disabled = true;
      errorBox('Formato no compatible. Usá PDF, JPG, JPEG, PNG o WEBP.');
      return;
    }
    if (!file.size || file.size > 10 * 1024 * 1024) {
      currentFile = null;
      if (process) process.disabled = true;
      if (clear) clear.disabled = true;
      errorBox('El archivo debe pesar entre 1 byte y 10 MB.');
      return;
    }
    if (selected) {
      const sizeKb = Math.max(1, Math.round(file.size / 1024));
      const isImage = String(file.type || '').startsWith('image/') || ['.jpg', '.jpeg', '.png', '.webp'].includes(extension);
      if (isImage) {
        previewObjectUrl = URL.createObjectURL(file);
        selected.innerHTML = `<div class="d-flex align-items-center gap-3 flex-wrap"><img src="${previewObjectUrl}" alt="Vista previa" style="width:96px;height:96px;object-fit:cover;border-radius:12px;border:1px solid var(--app-line)"><div><div class="fw-semibold">${esc(filename)}</div><div class="small muted">${sizeKb} KB</div><div class="small text-success mt-1"><i class="bi bi-check-circle me-1"></i>Lista para procesar con IA</div></div></div>`;
      } else {
        selected.innerHTML = `<div class="d-flex align-items-center gap-3"><div style="width:64px;height:64px;display:grid;place-items:center;border-radius:12px;background:#eef4ff;color:#2563eb;font-size:1.5rem"><i class="bi bi-file-earmark-pdf"></i></div><div><div class="fw-semibold">${esc(filename)}</div><div class="small muted">${sizeKb} KB</div><div class="small text-success mt-1"><i class="bi bi-check-circle me-1"></i>Lista para procesar con IA</div></div></div>`;
      }
    }
    if (process) process.disabled = false;
    if (clear) clear.disabled = false;
  }

  function clearFile() {
    releasePreview();
    currentFile = null;
    const file = $('invoice-file-input');
    const camera = $('invoice-camera-input');
    if (file) file.value = '';
    if (camera) camera.value = '';
    const selected = $('invoice-selected');
    if (selected) selected.textContent = 'Todavía no seleccionaste una factura.';
    const process = $('invoice-process');
    const clear = $('invoice-clear');
    if (process) process.disabled = true;
    if (clear) clear.disabled = true;
    clearError();
  }

  function linesFromPreview(preview) {
    return Array.isArray(preview?.matches) ? preview.matches : [];
  }

  function lineAssigned(line) {
    return Boolean(line?.product_id) && ['MATCH_EXACTO', 'APLICADO'].includes(String(line?.matching_status || ''));
  }

  function invoiceReadiness(preview) {
    const lines = linesFromPreview(preview).filter((line) => line.matching_status !== 'EXCLUIDA');
    const supplierStatus = String(preview?.supplier_match?.status || '');
    const supplierReady = ['MATCH_EXACTO', 'NUEVO_PROVEEDOR_CONFIRMADO'].includes(supplierStatus);
    const allAssigned = lines.length > 0 && lines.every(lineAssigned);
    const hasSubtotal = preview?.subtotal !== null && preview?.subtotal !== undefined && preview?.subtotal !== '';
    const hasTotal = preview?.total !== null && preview?.total !== undefined && preview?.total !== '';
    const currency = String(preview?.currency || '').toUpperCase();
    const currencyReady = currency === 'ARS';
    const warnings = Array.isArray(preview?.warnings) ? preview.warnings : [];
    const blockingWarnings = warnings.some((warning) => String(warning).startsWith('La línea ') || String(warning).startsWith('Los totales de la factura'));
    return {
      lines,
      supplierReady,
      allAssigned,
      hasSubtotal,
      hasTotal,
      currencyReady,
      blockingWarnings,
      ready: supplierReady && allAssigned && hasSubtotal && hasTotal && currencyReady && !blockingWarnings,
    };
  }

  function assignmentSummary(preview) {
    const lines = linesFromPreview(preview);
    const active = lines.filter((line) => line.matching_status !== 'EXCLUIDA');
    const assigned = active.filter(lineAssigned).length;
    const excluded = lines.filter((line) => line.matching_status === 'EXCLUIDA').length;
    return { total: active.length, assigned, excluded };
  }

  function renderAssignmentBanner(preview) {
    const box = $('review-kpis');
    if (!box) return;
    const existing = $('invoice-assignment-progress');
    if (existing) existing.remove();
    const summary = assignmentSummary(preview);
    const ready = invoiceReadiness(preview);
    const applied = String(preview?.status || '') === 'APLICADA';
    const progress = summary.total ? Math.round((summary.assigned / summary.total) * 100) : 0;
    const container = document.createElement('div');
    container.id = 'invoice-assignment-progress';
    container.className = 'col-12 mt-2';
    let message = applied
      ? '✅ Factura aplicada. Las líneas quedan bloqueadas para evitar inconsistencias.'
      : ready.ready
        ? '✅ Todos los productos están asignados. La factura está lista para confirmar.'
        : `Productos asignados: ${summary.assigned}/${summary.total}. Resolvé las líneas pendientes antes de aplicar.`;
    container.innerHTML = `<div class="invoice-kpi"><div class="d-flex flex-wrap justify-content-between align-items-center gap-2"><strong>${message}</strong><span class="small muted">${progress}%</span></div><div class="progress mt-2" style="height:8px"><div class="progress-bar" style="width:${progress}%"></div></div>${summary.excluded ? `<div class="small muted mt-2">${summary.excluded} línea(s) excluida(s).</div>` : ''}${!ready.currencyReady && !applied ? '<div class="small text-danger mt-2">La confirmación de compras IA requiere moneda ARS.</div>' : ''}${(!ready.hasSubtotal || !ready.hasTotal) && !applied ? '<div class="small text-danger mt-2">Falta subtotal o total de factura para confirmar.</div>' : ''}</div>`;
    box.parentElement?.insertAdjacentElement('afterend', container);
  }

  function renderSupplier(preview) {
    const target = $('review-supplier');
    if (!target) return;
    const supplier = preview?.supplier || {};
    const match = preview?.supplier_match || {};
    const status = String(match.status || 'PENDIENTE');
    const name = match.name || supplier.name || '';
    const applied = String(preview?.status || '') === 'APLICADA';
    let html = `<div class="d-flex flex-wrap justify-content-between gap-3"><div><div class="small muted">Proveedor detectado</div><strong>${esc(name || 'Sin reconocer')}</strong><div class="small muted mt-1">${esc(status.replaceAll('_', ' '))}</div></div>`;
    if (status === 'MATCH_EXACTO' || status === 'NUEVO_PROVEEDOR_CONFIRMADO') {
      html += `<div class="text-end"><span class="badge text-bg-success">${status === 'NUEVO_PROVEEDOR_CONFIRMADO' ? 'Proveedor listo para crear' : 'Proveedor listo'}</span></div></div>`;
      target.innerHTML = html;
      return;
    }
    if (applied) {
      target.innerHTML = html + '<div class="text-end"><span class="badge text-bg-secondary">Bloqueado · factura aplicada</span></div></div>';
      return;
    }
    const candidates = Array.isArray(match.candidates) ? match.candidates : [];
    html += '<div class="d-flex flex-column gap-2" style="min-width:min(100%,520px)">';
    if (candidates.length) {
      html += '<div class="small fw-semibold">Usar proveedor existente</div><div class="d-flex flex-wrap gap-2">' + candidates.map((item) => `<button type="button" class="btn btn-sm btn-outline-primary supplier-use" data-id="${esc(item.id)}">${esc(item.name)}</button>`).join('') + '</div>';
    }
    if (name) html += `<button type="button" class="btn btn-sm btn-primary supplier-create"><i class="bi bi-plus-circle me-1"></i>Crear proveedor "${esc(name)}"</button>`;
    html += '</div></div>';
    target.innerHTML = html;
  }

  function productDisplay(line) {
    if (!lineAssigned(line)) return '<div class="small muted">⚠️ Pendiente de asignar</div>';
    const cost = line.final_unit_cost ?? line.unit_cost ?? '—';
    const salePrice = line.sale_price;
    const saleLabel = salePrice !== null && salePrice !== undefined && salePrice !== ''
      ? `Precio venta: $${esc(salePrice)}`
      : 'Precio venta: pendiente / sin modificar';
    return `<div class="small fw-semibold">✅ ${esc(line.product_name || 'Producto')}</div><div class="small muted">Código: ${esc(line.product_code || 'sin código')}</div><div class="small text-primary">Costo final de compra: $${esc(cost)}</div><div class="small muted">${saleLabel}</div>`;
  }

  function renderRows(preview, uploadId) {
    const target = $('review-items');
    if (!target) return;
    const lines = linesFromPreview(preview);
    const applied = String(preview?.status || '') === 'APLICADA';
    target.innerHTML = lines.map((line) => {
      const assigned = lineAssigned(line);
      const excluded = line.matching_status === 'EXCLUIDA';
      const action = applied || excluded
        ? ''
        : `<button type="button" class="btn btn-sm ${assigned ? 'btn-outline-secondary' : 'btn-outline-primary'} picker-open" data-line="${esc(line.line_number)}">${assigned ? 'Cambiar' : 'Asignar / escanear'}</button>`;
      const stateClass = excluded ? 'text-bg-secondary' : assigned ? 'text-bg-success' : 'text-bg-warning text-dark';
      const stateText = excluded ? 'EXCLUIDA' : line.matching_status === 'APLICADO' ? 'APLICADO' : assigned ? 'ASIGNADO' : 'PENDIENTE';
      return `<tr><td><strong>${esc(line.description || 'Sin descripción')}</strong><div class="small muted">Código factura: ${esc(line.code || line.barcode || 'sin código')}</div></td><td>${productDisplay(line)}</td><td><span class="badge ${stateClass}">${stateText}</span></td><td>${esc(line.quantity)}</td><td>$${esc(line.final_unit_cost ?? line.unit_cost ?? '—')}<div class="small muted">Línea: $${esc(line.line_total ?? '—')}</div></td><td class="text-end">${action}</td></tr>`;
    }).join('');
  }

  function renderReview(preview, uploadId, name = 'Factura') {
    currentUploadId = uploadId;
    currentPreview = preview || {};
    const box = $('invoice-review');
    if (!box) return;
    const invoice = currentPreview.invoice || currentPreview;
    currentPreview = invoice || {};
    box.classList.remove('d-none');
    const title = $('review-title');
    const status = $('review-status');
    if (title) title.textContent = invoice.invoice_number ? `Factura ${invoice.invoice_number}` : name;
    if (status) status.textContent = String(invoice.status || 'REQUIERE_REVISION').replaceAll('_', ' ');
    const supplier = invoice.supplier || invoice.supplier_match || {};
    const kpis = $('review-kpis');
    if (kpis) {
      kpis.innerHTML = [
        ['Proveedor', supplier.name || 'Sin reconocer'],
        ['Fecha', invoice.issue_date || '—'],
        ['Total', invoice.total !== null && invoice.total !== undefined ? `$${invoice.total}` : '—'],
        ['Líneas', linesFromPreview(invoice).length],
      ].map(([label, value]) => `<div class="col-6 col-lg-3"><div class="invoice-kpi"><div class="small muted">${esc(label)}</div><strong>${esc(value)}</strong></div></div>`).join('');
    }
    renderAssignmentBanner(invoice);
    renderSupplier(invoice);
    renderRows(invoice, uploadId);
    const confirm = $('review-confirm');
    const readiness = invoiceReadiness(invoice);
    const applied = String(invoice.status || '') === 'APLICADA';
    if (confirm) {
      confirm.disabled = applied || !readiness.ready;
      confirm.textContent = applied ? '✅ Aplicada' : 'Confirmar y aplicar al stock';
    }
    const alert = $('review-alert');
    if (alert) {
      alert.className = 'alert d-none';
      alert.textContent = '';
    }
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function openPicker(uploadId, lineNumber) {
    const line = linesFromPreview(currentPreview).find((item) => String(item.line_number) === String(lineNumber));
    if (!line || String(currentPreview?.status || '') === 'APLICADA') return;
    pickerLine = lineNumber;
    currentCandidates = Array.isArray(line.candidate_products) ? line.candidate_products : [];
    const title = $('picker-title');
    const query = $('picker-query');
    const code = $('picker-code');
    if (title) title.textContent = line.description || 'Producto';
    if (query) query.value = '';
    if (code) { code.value = ''; code.placeholder = 'Código / SKU'; code.focus(); }
    const hint = $('picker-code-hint');
    if (hint) hint.textContent = 'Escribí el código, escanealo con cámara o usá un lector láser/USB/Bluetooth. La línea queda marcada como asignada sin sumar stock todavía.';
    renderCandidates(currentCandidates);
    $('invoice-picker')?.classList.add('show');
  }

  function renderCandidates(candidates) {
    const list = $('picker-list');
    if (!list) return;
    list.innerHTML = candidates.length
      ? candidates.map((item) => `<div class="candidate"><div><strong>${esc(item.name)}</strong><div class="small muted">${esc(item.code || 'Sin código')}${item.category ? ` · ${esc(item.category)}` : ''}</div></div><button type="button" class="btn btn-sm btn-primary picker-use" data-product-id="${esc(item.id)}" data-code="${esc(item.code || '')}">Usar</button></div>`).join('')
      : '<div class="small muted">No encontramos sugerencias. Escribí el código exacto, usá la cámara o conectá el lector.</div>';
  }

  async function assignCandidate(button) {
    const code = String(button.dataset.code || '').trim();
    if (!code) throw new Error('La sugerencia seleccionada no tiene código utilizable.');
    await assignCode(code);
  }

  async function assignCode(code) {
    const value = String(code || '').trim();
    if (!value) throw new Error('Escribí o escaneá el código/SKU del producto.');
    if (!currentUploadId || pickerLine === null) throw new Error('No pude identificar la línea de factura activa.');
    if (String(currentPreview?.status || '') === 'APLICADA') throw new Error('La factura ya fue aplicada y está bloqueada.');
    const response = await fetch(directCodeUrl().replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId)), postOptions({ line_number: pickerLine, code: value }));
    const data = await jsonResponse(response);
    currentPreview = data.preview || currentPreview;
    $('invoice-picker')?.classList.remove('show');
    renderReview(currentPreview, currentUploadId, 'Factura');
  }

  async function supplierDecision(payload) {
    if (!currentUploadId) throw new Error('No pude identificar la factura activa.');
    const response = await fetch(resolveUrl.replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId)), postOptions(payload));
    const data = await jsonResponse(response);
    currentPreview = data.preview || currentPreview;
    renderReview(currentPreview, currentUploadId, 'Factura');
  }

  async function uploadAndProcess() {
    if (!currentFile) {
      errorBox('Elegí una factura o sacale una foto primero.');
      return;
    }
    clearError();
    const process = $('invoice-process');
    const clear = $('invoice-clear');
    if (process) process.disabled = true;
    if (clear) clear.disabled = true;
    setProgress(20, true);
    try {
      const form = new FormData();
      form.append('message', 'Cargué una factura de proveedor para procesarla con IA.');
      form.append('agent', 'asistente');
      form.append('invoice_file', currentFile, currentFile.name);
      const uploaded = await jsonResponse(await fetch(uploadUrl, {
        method: 'POST',
        body: form,
        credentials: 'same-origin',
        headers: { 'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest' },
      }));
      currentUploadId = uploaded.document_id;
      setProgress(55, true);
      const processed = await jsonResponse(await fetch(processUrl.replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId)), postOptions()));
      setProgress(100, true);
      renderReview(processed.preview || {}, currentUploadId, currentFile.name);
      setTimeout(() => setProgress(0, false), 500);
    } catch (error) {
      errorBox(error.message);
      setProgress(0, false);
    } finally {
      if (process) process.disabled = !currentFile;
      if (clear) clear.disabled = !currentFile;
    }
  }

  async function openInvoice(uploadId) {
    clearError();
    try {
      const data = await jsonResponse(await fetch(previewUrl.replace('__UPLOAD_ID__', encodeURIComponent(uploadId)), { credentials: 'same-origin' }));
      renderReview(data.preview || {}, uploadId, data.original_name || 'Factura');
    } catch (error) {
      errorBox(error.message);
    }
  }

  async function confirmInvoice() {
    if (!currentUploadId) return;
    const readiness = invoiceReadiness(currentPreview || {});
    if (!readiness.ready) {
      errorBox('La factura todavía no está completa: resolvé todos los productos, proveedor, subtotal, total y moneda ARS antes de aplicar.');
      return;
    }
    const button = $('review-confirm');
    if (button) button.disabled = true;
    clearError();
    try {
      const data = await jsonResponse(await fetch(confirmUrl.replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId)), postOptions()));
      currentPreview = data.preview || currentPreview;
      currentPreview.status = 'APLICADA';
      const result = data.result || {};
      currentPreview.result = result;
      renderReview(currentPreview, currentUploadId, 'Factura');
      const alert = $('review-alert');
      if (alert) {
        alert.className = 'alert alert-success';
        alert.textContent = data.duplicate ? 'Esta factura ya estaba aplicada anteriormente.' : '✅ Factura aplicada. Compra, proveedor, costos y stock actualizados.';
        alert.classList.remove('d-none');
      }
      setTimeout(() => window.location.reload(), 1300);
    } catch (error) {
      errorBox(error.message);
      if (button) button.disabled = false;
    }
  }

  async function ensureBarcodeScanner() {
    if (window.StockArBarcodeScanner?.openScanner) return true;
    if (window.__stockArBarcodeScannerLoader) return window.__stockArBarcodeScannerLoader;
    window.__stockArBarcodeScannerLoader = new Promise((resolve) => {
      const script = document.createElement('script');
      script.src = '/static/assets/js/barcode-scanner.js?v=20260917-final-1';
      script.onload = () => resolve(Boolean(window.StockArBarcodeScanner?.openScanner));
      script.onerror = () => resolve(false);
      document.head.appendChild(script);
    });
    return window.__stockArBarcodeScannerLoader;
  }

  async function openCamera() {
    clearError();
    const ready = await ensureBarcodeScanner();
    if (!ready) {
      errorBox('No se pudo cargar el escáner de cámara. Podés usar el lector láser o escribir el código.');
      return;
    }
    window.StockArBarcodeScanner.openScanner({
      onDetected: async (code) => {
        const input = $('picker-code');
        if (input) input.value = String(code || '').trim();
        try {
          await assignCode(code);
        } catch (error) {
          errorBox(error.message);
        }
      },
      onError: (error) => errorBox(error?.message || 'No se pudo abrir la cámara.'),
      onCancel: () => {},
    });
  }

  function focusLaser() {
    const input = $('picker-code');
    if (!input) return;
    input.value = '';
    input.placeholder = 'Esperando lector láser…';
    input.focus();
    const hint = $('picker-code-hint');
    if (hint) hint.textContent = 'Lector activo: escaneá el código de barras. Al terminar con Enter, StockAR lo asigna a esta línea.';
  }

  function handleClick(event) {
    const target = event.target;
    const button = target.closest?.('button, .invoice-open, label');
    if (!button) return;

    if (button.id === 'invoice-process') {
      event.preventDefault();
      event.stopImmediatePropagation();
      uploadAndProcess();
      return;
    }
    if (button.id === 'invoice-clear') {
      event.preventDefault();
      event.stopImmediatePropagation();
      clearFile();
      return;
    }
    if (button.classList.contains('invoice-open')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      openInvoice(button.dataset.uploadId || '');
      return;
    }
    if (button.classList.contains('picker-open')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      openPicker(currentUploadId, button.dataset.line || '');
      return;
    }
    if (button.id === 'picker-close') {
      event.preventDefault();
      event.stopImmediatePropagation();
      $('invoice-picker')?.classList.remove('show');
      return;
    }
    if (button.id === 'picker-code-use') {
      event.preventDefault();
      event.stopImmediatePropagation();
      assignCode($('picker-code')?.value || '').catch((error) => errorBox(error.message));
      return;
    }
    if (button.id === 'picker-camera-open') {
      event.preventDefault();
      event.stopImmediatePropagation();
      openCamera();
      return;
    }
    if (button.id === 'picker-laser-focus') {
      event.preventDefault();
      event.stopImmediatePropagation();
      focusLaser();
      return;
    }
    if (button.classList.contains('picker-use')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      assignCandidate(button).catch((error) => errorBox(error.message));
      return;
    }
    if (button.classList.contains('supplier-use')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      supplierDecision({ target: 'supplier', decision: 'use_existing', supplier_id: button.dataset.id }).catch((error) => errorBox(error.message));
      return;
    }
    if (button.classList.contains('supplier-create')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      supplierDecision({ target: 'supplier', decision: 'create_new' }).catch((error) => errorBox(error.message));
      return;
    }
    if (button.id === 'review-confirm') {
      event.preventDefault();
      event.stopImmediatePropagation();
      confirmInvoice();
    }
  }

  function handleChange(event) {
    const target = event.target;
    if (!(target instanceof HTMLInputElement)) return;
    if (target.id === 'invoice-file-input' || target.id === 'invoice-camera-input') {
      event.stopImmediatePropagation();
      selectFile(target.files && target.files[0]);
    }
  }

  function handleKeydown(event) {
    if (event.target?.id === 'picker-code' && event.key === 'Enter') {
      event.preventDefault();
      event.stopImmediatePropagation();
      assignCode(event.target.value).catch((error) => errorBox(error.message));
    }
  }

  function handleInput(event) {
    if (event.target?.id !== 'picker-query') return;
    event.stopImmediatePropagation();
    const query = String(event.target.value || '').trim().toLowerCase();
    renderCandidates(currentCandidates.filter((item) => String(item.name || '').toLowerCase().includes(query) || String(item.code || '').toLowerCase().includes(query)));
  }

  function handleDrop(event) {
    const zone = event.target.closest?.('#invoice-dropzone');
    if (!zone) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.type === 'drop') selectFile(event.dataTransfer?.files?.[0] || null);
  }

  document.addEventListener('click', handleClick, true);
  document.addEventListener('change', handleChange, true);
  document.addEventListener('keydown', handleKeydown, true);
  document.addEventListener('input', handleInput, true);
  document.addEventListener('dragover', (event) => {
    if (event.target.closest?.('#invoice-dropzone')) event.preventDefault();
  }, true);
  document.addEventListener('drop', handleDrop, true);

  window.StockArInvoiceFlowV2 = {
    openInvoice,
    renderReview,
  };
})();
