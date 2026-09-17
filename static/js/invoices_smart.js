(() => {
  const root = document.getElementById('invoice-ai-root');
  if (!root) return;

  const $ = (id) => document.getElementById(id);
  const fileInput = $('invoice-file-input');
  const cameraInput = $('invoice-camera-input');
  const fileTrigger = $('invoice-file-trigger');
  const cameraTrigger = $('invoice-camera-trigger');
  const dropzone = $('invoice-dropzone');
  const selected = $('invoice-selected');
  const errorBox = $('invoice-error');
  const processBtn = $('invoice-process');
  const clearBtn = $('invoice-clear');
  const progress = $('invoice-progress');
  const progressBar = $('invoice-progress-bar');

  if (!fileInput || !cameraInput || !fileTrigger || !cameraTrigger || !selected || !processBtn || !clearBtn) return;

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

  const showError = (message) => {
    if (errorBox) {
      errorBox.textContent = message || 'No se pudo procesar la factura.';
      errorBox.classList.remove('d-none');
    }
  };

  const clearError = () => {
    if (errorBox) {
      errorBox.textContent = '';
      errorBox.classList.add('d-none');
    }
  };

  const releasePreviewUrl = () => {
    if (currentObjectUrl) {
      URL.revokeObjectURL(currentObjectUrl);
      currentObjectUrl = null;
    }
  };

  const esc = (value) => {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
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
    const type = String(file.type || '').toLowerCase();
    const isImage = type.startsWith('image/') || ['.jpg', '.jpeg', '.png', '.webp'].some((ext) => name.toLowerCase().endsWith(ext));

    if (isImage) {
      currentObjectUrl = URL.createObjectURL(file);
      selected.innerHTML =
        '<div class="d-flex align-items-center gap-3 flex-wrap">' +
        '<img src="' + currentObjectUrl + '" alt="Vista previa de la factura" style="width:96px;height:96px;object-fit:cover;border-radius:12px;border:1px solid var(--app-line);background:#f8fafc">' +
        '<div><div class="fw-semibold">' + esc(name) + '</div><div class="small muted">' + sizeKb + ' KB · Imagen seleccionada correctamente</div><div class="small text-success mt-1"><i class="bi bi-check-circle me-1"></i>Lista para procesar con IA</div></div>' +
        '</div>';
      return;
    }

    selected.innerHTML =
      '<div class="d-flex align-items-center gap-3 flex-wrap">' +
      '<div style="width:64px;height:64px;display:grid;place-items:center;border-radius:12px;background:#eef4ff;color:#2563eb;font-size:1.5rem"><i class="bi bi-file-earmark-pdf"></i></div>' +
      '<div><div class="fw-semibold">' + esc(name) + '</div><div class="small muted">' + sizeKb + ' KB · PDF seleccionado correctamente</div><div class="small text-success mt-1"><i class="bi bi-check-circle me-1"></i>Listo para procesar con IA</div></div>' +
      '</div>';
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
    const options = {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest' }
    };
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
    if (!currentFile) {
      showError('Elegí una factura o sacale una foto primero.');
      return;
    }

    processBtn.disabled = true;
    clearBtn.disabled = true;
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
        headers: { 'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest' }
      }));

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

  function renderRows(preview, uploadId) {
    const target = $('review-items');
    const rows = preview.matches || [];
    if (!target) return;

    target.innerHTML = rows.map((line) => {
      const status = line.matching_status;
      const automatic = status === 'MATCH_EXACTO';
      const unresolved = ['AMBIGUO', 'MATCH_PROPUESTO'].includes(status);
      const isNew = status === 'NUEVO_PRODUCTO';
      let action = '<span class="small text-success fw-semibold"><i class="bi bi-check-circle me-1"></i>Listo</span>';
      if (automatic) action = '<span class="small text-success fw-semibold"><i class="bi bi-stars me-1"></i>Automático</span>';
      if (unresolved) action = '<button type="button" class="btn btn-sm btn-outline-primary picker-open" data-line="' + esc(line.line_number) + '">Elegir producto</button>';
      if (isNew) action = '<span class="small text-muted">Se creará al confirmar</span>';

      const product = line.product_name ? '<div class="small">' + esc(line.product_name) + '</div>' : '<div class="small muted">Sin vincular</div>';
      const confidenceClass = line.confidence_level === 'ALTA' ? 'text-bg-success' : line.confidence_level === 'MEDIA' ? 'text-bg-warning text-dark' : 'text-bg-secondary';

      return '<tr>' +
        '<td><strong>' + esc(line.description || 'Sin descripción') + '</strong><div class="small muted">' + esc(line.code || line.barcode || '') + '</div></td>' +
        '<td>' + product + '</td>' +
        '<td><span class="badge ' + confidenceClass + '">' + esc(line.confidence_level || 'BAJA') + (line.proposal_score ? ' · ' + Math.round(line.proposal_score * 100) + '%' : '') + '</span></td>' +
        '<td>' + esc(line.quantity) + '</td>' +
        '<td>' + esc(line.unit_cost) + '</td>' +
        '<td class="text-end">' + action + '</td>' +
        '</tr>';
    }).join('');

    target.querySelectorAll('.picker-open').forEach((button) => {
      button.addEventListener('click', () => openPicker(uploadId, button.dataset.line));
    });
  }

  function renderReview(preview, uploadId, name) {
    currentUploadId = uploadId;
    currentPreview = preview;
    const box = $('invoice-review');
    if (!box) return;
    const invoice = preview.invoice || {};
    const supplier = preview.supplier || {};
    const supplierMatch = preview.supplier_match || {};
    box.classList.remove('d-none');

    $('review-title').textContent = invoice.number ? 'Factura ' + invoice.number : (name || 'Factura');
    $('review-status').textContent = String(preview.status || 'REQUIERE_REVISION').replaceAll('_', ' ');
    $('review-kpis').innerHTML = [
      ['Proveedor', supplier.name || supplierMatch.name || 'Sin reconocer'],
      ['Fecha', invoice.date || '—'],
      ['Total', invoice.total ?? '—'],
      ['Líneas', (preview.matches || []).length]
    ].map((item) => '<div class="col-6 col-lg-3"><div class="invoice-kpi"><div class="small muted">' + esc(item[0]) + '</div><strong>' + esc(item[1]) + '</strong></div></div>').join('');
    $('review-supplier').innerHTML = '<div class="d-flex flex-wrap justify-content-between gap-2"><div><div class="small muted">Proveedor detectado</div><strong>' + esc(supplier.name || supplierMatch.name || 'Sin reconocer') + '</strong></div><div><div class="small muted">Estado</div><strong>' + esc(supplierMatch.status || 'Pendiente') + '</strong></div></div>';
    renderRows(preview, uploadId);
    const confirm = $('review-confirm');
    if (confirm) confirm.disabled = preview.status !== 'LISTA_PARA_CONFIRMAR';
    box.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function openPicker(uploadId, lineNumber) {
    const line = (currentPreview && currentPreview.matches || []).find((item) => String(item.line_number) === String(lineNumber));
    if (!line) return;
    pickerLine = lineNumber;
    currentCandidates = line.candidate_products || [];
    $('picker-title').textContent = line.description || 'Producto';
    $('picker-query').value = '';
    renderCandidates(currentCandidates);
    $('invoice-picker').classList.add('show');
  }

  function renderCandidates(candidates) {
    const list = $('picker-list');
    list.innerHTML = candidates.length ? candidates.map((item) => '<div class="candidate"><div><strong>' + esc(item.name) + '</strong><div class="small muted">' + esc(item.code || 'Sin código') + (item.category ? ' · ' + esc(item.category) : '') + '</div></div><button type="button" class="btn btn-sm btn-primary picker-use" data-name="' + esc(item.name) + '">Usar</button></div>').join('') : '<div class="small muted">No encontramos sugerencias para esta línea.</div>';
    list.querySelectorAll('.picker-use').forEach((button) => button.addEventListener('click', async () => {
      try {
        const url = resolveUrl.replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId));
        const data = await jsonResponse(await fetch(url, postOptions({ line_number: pickerLine, description: button.dataset.name })));
        $('invoice-picker').classList.remove('show');
        renderReview(data.preview || {}, currentUploadId, 'Factura');
      } catch (error) {
        showError(error.message);
      }
    }));
  }

  async function openInvoice(uploadId) {
    try {
      const data = await jsonResponse(await fetch(previewUrl.replace('__UPLOAD_ID__', encodeURIComponent(uploadId)), { credentials: 'same-origin' }));
      renderReview(data.preview || {}, uploadId, data.original_name || 'Factura');
    } catch (error) { showError(error.message); }
  }

  fileTrigger.addEventListener('click', () => fileInput.click());
  cameraTrigger.addEventListener('click', () => cameraInput.click());
  fileInput.addEventListener('change', () => selectFile(fileInput.files && fileInput.files[0]));
  cameraInput.addEventListener('change', () => selectFile(cameraInput.files && cameraInput.files[0]));
  processBtn.addEventListener('click', uploadAndProcess);
  clearBtn.addEventListener('click', clearFile);

  document.querySelectorAll('.invoice-open').forEach((button) => button.addEventListener('click', () => openInvoice(button.dataset.uploadId)));
  $('picker-close')?.addEventListener('click', () => $('invoice-picker').classList.remove('show'));
  $('picker-query')?.addEventListener('input', () => {
    const query = $('picker-query').value.trim().toLowerCase();
    renderCandidates(currentCandidates.filter((item) => String(item.name || '').toLowerCase().includes(query) || String(item.code || '').toLowerCase().includes(query)));
  });

  $('review-confirm')?.addEventListener('click', async () => {
    const button = $('review-confirm');
    button.disabled = true;
    try {
      const data = await jsonResponse(await fetch(confirmUrl.replace('__UPLOAD_ID__', encodeURIComponent(currentUploadId)), postOptions()));
      const alert = $('review-alert');
      alert.className = 'alert alert-success';
      alert.textContent = data.duplicate ? 'Esta factura ya estaba aplicada anteriormente.' : 'Factura confirmada. La compra y el stock fueron actualizados.';
      alert.classList.remove('d-none');
      setTimeout(() => window.location.reload(), 900);
    } catch (error) {
      showError(error.message);
      button.disabled = false;
    }
  });

  dropzone?.addEventListener('dragover', (event) => { event.preventDefault(); dropzone.classList.add('dragover'); });
  dropzone?.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
  dropzone?.addEventListener('drop', (event) => {
    event.preventDefault();
    dropzone.classList.remove('dragover');
    selectFile(event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0]);
  });
})();
