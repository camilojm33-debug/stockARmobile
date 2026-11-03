(() => {
  const root = document.getElementById('branch-transfer-manager');
  if (!root) return;

  const form = document.getElementById('branch-transfer-form');
  const source = document.getElementById('branch-transfer-source');
  const target = document.getElementById('branch-transfer-target');
  const search = document.getElementById('branch-transfer-search');
  const category = document.getElementById('branch-transfer-category');
  const availableOnly = document.getElementById('branch-transfer-available-only');
  const results = document.getElementById('branch-transfer-results');
  const selectedBody = document.getElementById('branch-transfer-selected');
  const status = document.getElementById('branch-transfer-search-status');
  const resultCount = document.getElementById('branch-transfer-result-count');
  const summary = document.getElementById('branch-transfer-summary');
  const loadMoreButton = document.getElementById('branch-transfer-load-more');
  const addVisibleButton = document.getElementById('branch-transfer-add-visible');
  const submitButton = document.getElementById('branch-transfer-submit');
  const submitStatus = document.getElementById('branch-transfer-submit-status');
  const itemsJson = document.getElementById('branch-transfer-items-json');
  const pageSize = 30;
  const selected = new Map();
  let visibleProducts = [];
  let offset = 0;
  let total = 0;
  let requestVersion = 0;
  let searchTimer = null;
  let lastTargetId = target.value;

  const escapeHtml = value => {
    const node = document.createElement('div');
    node.textContent = value == null ? '' : String(value);
    return node.innerHTML;
  };

  const quantityText = value => Number(value || 0).toLocaleString('es-AR', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 3
  });

  const stockNumber = value => {
    const parsed = Number(value);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : 0;
  };

  function quantityIsValid(value, max) {
    const normalized = String(value == null ? '' : value).trim().replace(',', '.');
    if (!normalized || !/^\d+(?:\.\d{1,3})?$/.test(normalized)) return false;
    const quantity = Number(normalized);
    return Number.isFinite(quantity) && quantity > 0 && quantity <= max;
  }

  function updateSummary() {
    let units = 0;
    let valid = selected.size > 0;
    selected.forEach(item => {
      if (!quantityIsValid(item.quantity, item.stock)) {
        valid = false;
      } else {
        units += Number(String(item.quantity).replace(',', '.'));
      }
    });
    summary.textContent = selected.size + ' producto(s) · ' + quantityText(units) + ' unidades';
    submitButton.disabled = !valid || source.value === target.value;
    if (source.value === target.value) {
      submitStatus.textContent = 'El origen y el destino deben ser sucursales distintas.';
    } else if (!selected.size) {
      submitStatus.textContent = 'Agregá productos para preparar la transferencia.';
    } else if (!valid) {
      submitStatus.textContent = 'Revisá las cantidades: deben ser mayores a cero y no superar el stock disponible.';
    } else {
      submitStatus.textContent = 'Todo listo. El stock se valida nuevamente al confirmar.';
    }
  }

  function renderResults() {
    if (!visibleProducts.length) {
      results.innerHTML = '<div class="list-group-item small text-muted">No hay productos que coincidan con la búsqueda y los filtros.</div>';
      addVisibleButton.disabled = true;
    } else {
      results.innerHTML = visibleProducts.map(product => {
        const id = Number(product.id);
        const added = selected.has(id);
        const stock = stockNumber(product.stock);
        const disabled = added || stock <= 0;
        return '<div class="list-group-item d-flex flex-wrap justify-content-between align-items-center gap-2">' +
          '<div class="me-auto"><div class="fw-semibold">' + escapeHtml(product.name) + '</div>' +
          '<div class="small text-muted">' + escapeHtml(product.barcode || 'Sin código') +
          (product.category ? ' · ' + escapeHtml(product.category) : '') +
          (product.brand ? ' · ' + escapeHtml(product.brand) : '') + '</div></div>' +
          '<div class="text-end small me-2"><div class="fw-semibold">' + quantityText(stock) + ' ' + escapeHtml(product.unit_measure || 'u') + '</div><div class="text-muted">Stock en origen</div></div>' +
          '<button type="button" class="btn btn-sm ' + (added ? 'btn-success' : 'btn-outline-primary') +
          '" data-transfer-add="' + id + '" ' + (disabled ? 'disabled' : '') + '>' + (added ? 'Agregado' : stock > 0 ? 'Agregar' : 'Sin stock') + '</button>' +
          '</div>';
      }).join('');
      addVisibleButton.disabled = !visibleProducts.some(product => stockNumber(product.stock) > 0);
    }
    resultCount.textContent = total ? 'Mostrando ' + visibleProducts.length + ' de ' + total + ' productos' : '0 productos';
    loadMoreButton.classList.toggle('d-none', offset >= total);
  }

  function renderSelected() {
    if (!selected.size) {
      selectedBody.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-3">Todavía no agregaste productos.</td></tr>';
      updateSummary();
      renderResults();
      return;
    }
    selectedBody.innerHTML = Array.from(selected.values()).map(item => {
      const id = Number(item.id);
      return '<tr>' +
        '<td><div class="fw-semibold">' + escapeHtml(item.name) + '</div>' +
        '<div class="small text-muted">' + escapeHtml(item.barcode || 'Sin código') +
        (item.category ? ' · ' + escapeHtml(item.category) : '') + '</div></td>' +
        '<td class="text-end text-nowrap">' + quantityText(item.stock) + ' ' + escapeHtml(item.unit_measure || 'u') + '</td>' +
        '<td><input class="form-control form-control-sm" type="number" min="0.001" step="0.001" max="' + item.stock +
        '" value="' + escapeHtml(item.quantity) + '" data-transfer-quantity="' + id + '" aria-label="Cantidad a transferir de ' + escapeHtml(item.name) + '"></td>' +
        '<td class="text-end text-nowrap">' +
        '<button type="button" class="btn btn-sm btn-outline-secondary me-1" data-transfer-all="' + id + '" title="Transferir todo el stock disponible">Todo</button>' +
        '<button type="button" class="btn btn-sm btn-outline-danger" data-transfer-remove="' + id + '" aria-label="Quitar ' + escapeHtml(item.name) + '">Quitar</button>' +
        '</td></tr>';
    }).join('');
    updateSummary();
    renderResults();
  }

  function addProduct(product) {
    const id = Number(product && product.id || 0);
    const stock = stockNumber(product && product.stock);
    if (!id || stock <= 0) return;
    if (selected.has(id)) {
      const input = selectedBody.querySelector('[data-transfer-quantity="' + id + '"]');
      if (input) input.focus();
      status.textContent = 'Ese producto ya está en la lista; podés modificar su cantidad.';
      return;
    }
    const defaultQuantity = Math.min(1, stock);
    selected.set(id, {
      id,
      name: product.name || 'Producto',
      barcode: product.barcode || '',
      category: product.category || '',
      unit_measure: product.unit_measure || 'u',
      stock,
      quantity: String(Number(defaultQuantity.toFixed(3)))
    });
    renderSelected();
    status.textContent = product.name + ' agregado a la transferencia.';
  }

  async function loadProducts({ append = false, autoAddExact = false } = {}) {
    const version = ++requestVersion;
    if (!append) {
      offset = 0;
      visibleProducts = [];
    }
    status.textContent = 'Buscando productos...';
    loadMoreButton.disabled = true;
    const params = new URLSearchParams({
      source_branch_id: source.value,
      q: search.value.trim(),
      category: category.value,
      available_only: availableOnly.checked ? '1' : '0',
      offset: String(append ? offset : 0),
      limit: String(pageSize)
    });
    try {
      const response = await fetch(root.dataset.productsUrl + '?' + params.toString(), {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json', 'Cache-Control': 'no-cache' }
      });
      const payload = await response.json();
      if (version !== requestVersion) return;
      if (!response.ok || !payload.success) throw new Error(payload.error || 'No se pudieron cargar los productos.');
      const oldCategory = category.value;
      category.innerHTML = '<option value="">Todas las categorías</option>' +
        (payload.categories || []).map(name => '<option value="' + escapeHtml(name) + '">' + escapeHtml(name) + '</option>').join('');
      if (oldCategory && Array.from(category.options).some(option => option.value === oldCategory)) {
        category.value = oldCategory;
      }
      const rows = Array.isArray(payload.products) ? payload.products : [];
      visibleProducts = append ? visibleProducts.concat(rows) : rows;
      offset = Number(payload.offset || 0) + rows.length;
      total = Number(payload.total || 0);
      status.textContent = total
        ? 'Stock disponible en ' + (source.options[source.selectedIndex]?.text || 'origen') + '. Elegí uno o varios productos.'
        : 'No hay productos para mostrar con estos filtros.';
      renderResults();

      if (autoAddExact) {
        const entered = search.value.trim().toLocaleLowerCase();
        const exact = rows.filter(product => String(product.barcode || '').trim().toLocaleLowerCase() === entered);
        if (exact.length === 1 && stockNumber(exact[0].stock) > 0) {
          addProduct(exact[0]);
          search.value = '';
          await loadProducts();
          return;
        }
      }
    } catch (error) {
      if (version !== requestVersion) return;
      status.textContent = error.message || 'Error al buscar productos.';
      results.innerHTML = '<div class="list-group-item small text-danger">' + escapeHtml(status.textContent) + '</div>';
      resultCount.textContent = '';
      addVisibleButton.disabled = true;
      loadMoreButton.classList.add('d-none');
    } finally {
      if (version === requestVersion) loadMoreButton.disabled = false;
    }
  }

  function ensureDifferentBranches(changed) {
    if (source.value !== target.value) {
      lastTargetId = target.value;
      return true;
    }
    if (changed === 'target') {
      target.value = lastTargetId && lastTargetId !== source.value
        ? lastTargetId
        : Array.from(target.options).find(option => option.value !== source.value)?.value || '';
    } else {
      const replacement = Array.from(target.options).find(option => option.value !== source.value);
      if (replacement) target.value = replacement.value;
    }
    status.textContent = 'Elegí sucursales distintas para realizar la transferencia.';
    return source.value !== target.value;
  }

  search.addEventListener('input', () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => loadProducts(), 220);
  });
  search.addEventListener('keydown', event => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    loadProducts({ autoAddExact: true });
  });
  category.addEventListener('change', () => loadProducts());
  availableOnly.addEventListener('change', () => loadProducts());
  source.addEventListener('change', () => {
    ensureDifferentBranches('source');
    selected.clear();
    renderSelected();
    loadProducts();
  });
  target.addEventListener('change', () => ensureDifferentBranches('target'));

  results.addEventListener('click', event => {
    const button = event.target.closest('[data-transfer-add]');
    if (!button) return;
    const id = Number(button.dataset.transferAdd);
    const product = visibleProducts.find(item => Number(item.id) === id);
    if (product) addProduct(product);
  });

  addVisibleButton.addEventListener('click', () => {
    let added = 0;
    visibleProducts.forEach(product => {
      if (!selected.has(Number(product.id)) && stockNumber(product.stock) > 0) {
        addProduct(product);
        added += 1;
      }
    });
    status.textContent = added
      ? added + ' producto(s) agregado(s).'
      : 'No hay productos nuevos con stock disponible en los resultados visibles.';
  });

  loadMoreButton.addEventListener('click', () => loadProducts({ append: true }));

  selectedBody.addEventListener('input', event => {
    const input = event.target.closest('[data-transfer-quantity]');
    if (!input) return;
    const item = selected.get(Number(input.dataset.transferQuantity));
    if (!item) return;
    item.quantity = input.value;
    updateSummary();
  });

  selectedBody.addEventListener('click', event => {
    const removeButton = event.target.closest('[data-transfer-remove]');
    if (removeButton) {
      selected.delete(Number(removeButton.dataset.transferRemove));
      renderSelected();
      return;
    }
    const allButton = event.target.closest('[data-transfer-all]');
    if (allButton) {
      const item = selected.get(Number(allButton.dataset.transferAll));
      if (!item) return;
      item.quantity = String(Number(Number(item.stock).toFixed(3)));
      const input = selectedBody.querySelector('[data-transfer-quantity="' + item.id + '"]');
      if (input) input.value = item.quantity;
      updateSummary();
    }
  });

  form.addEventListener('submit', event => {
    const entries = Array.from(selected.values());
    const valid = entries.length > 0 && source.value !== target.value &&
      entries.every(item => quantityIsValid(item.quantity, item.stock));
    if (!valid) {
      event.preventDefault();
      updateSummary();
      return;
    }
    itemsJson.value = JSON.stringify(entries.map(item => ({
      product_id: item.id,
      quantity: String(item.quantity).replace(',', '.')
    })));
    submitButton.disabled = true;
    submitButton.textContent = 'Confirmando...';
    submitStatus.textContent = 'Validando stock y registrando todas las líneas...';
  });

  ensureDifferentBranches('source');
  renderSelected();
  loadProducts();
})();
