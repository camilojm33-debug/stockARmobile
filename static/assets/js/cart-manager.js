// CartManager - Gestor del Carrito de Compras
// Guarda y maneja productos en localStorage
let cart = [];
const CART_KEY_PREFIX = 'stockarmobile_cart';
let scannerStream = null;
let scannerLoopActive = false;
let html5QrScanner = null;

const checkoutProcessButton = document.getElementById('checkout-process-button');
const mpQrPanelTitle = document.getElementById('mp-qr-panel-title');
const mpQrPanelBadge = document.getElementById('mp-qr-panel-badge');
const mpQrPanelAmount = document.getElementById('mp-qr-panel-amount');
const mpQrPanelStatus = document.getElementById('mp-qr-panel-status');
const mpQrPanelImage = document.getElementById('mp-qr-panel-image');
const mpQrPanelOpen = document.getElementById('mp-qr-panel-open');
const mpQrPanelOperation = document.getElementById('mp-qr-panel-operation');
const mpQrPanelDateTime = document.getElementById('mp-qr-panel-datetime');
const mpQrPanel = document.getElementById('checkout-qr-panel');
const mpQrModalImage = document.getElementById('mp-qr-modal-image');
const mpQrModalStatus = document.getElementById('mp-qr-modal-status');
const mpQrPosSelect = document.getElementById('checkout-mp-pos-id');
const mpQrPosHelp = document.getElementById('checkout-mp-pos-help');
const mpQrPosSelectedName = document.getElementById('checkout-mp-pos-selected-name');
const mpQrPosSelectedStatus = document.getElementById('checkout-mp-pos-selected-status');

let mpQrDraftState = {
  paymentId: null,
  statusUrl: null,
  finalizeUrl: null,
  checkoutUrl: null,
  qrDataUri: null,
  pollTimer: null,
  approved: false,
};
let mpPosCatalogLoaded = false;
let mpPosCatalogLoading = false;

function normalizePosStatus(value) {
  return String(value || '').trim().toLowerCase();
}

function getSelectedPosMeta() {
  if (!mpQrPosSelect) return null;
  const selectedOption = mpQrPosSelect.options[mpQrPosSelect.selectedIndex];
  if (!selectedOption || !selectedOption.value) return null;
  return {
    id: String(selectedOption.value || '').trim(),
    name: String(selectedOption.dataset.name || selectedOption.textContent || '').trim(),
    status: normalizePosStatus(selectedOption.dataset.status),
    storeName: String(selectedOption.dataset.storeName || '').trim(),
  };
}

function updateSelectedPosBadge() {
  if (!mpQrPosSelectedName || !mpQrPosSelectedStatus) return;
  const selected = getSelectedPosMeta();
  if (!selected) {
    mpQrPosSelectedName.textContent = 'Sin seleccionar';
    mpQrPosSelectedStatus.textContent = 'Sin estado';
    mpQrPosSelectedStatus.className = 'badge text-bg-secondary';
    return;
  }

  mpQrPosSelectedName.textContent = selected.storeName
    ? `${selected.name} (${selected.storeName})`
    : selected.name;

  if (selected.status === 'active') {
    mpQrPosSelectedStatus.textContent = 'Activo';
    mpQrPosSelectedStatus.className = 'badge text-bg-success';
    return;
  }
  if (selected.status) {
    mpQrPosSelectedStatus.textContent = selected.status.toUpperCase();
    mpQrPosSelectedStatus.className = 'badge text-bg-warning';
    return;
  }
  mpQrPosSelectedStatus.textContent = 'Sin estado';
  mpQrPosSelectedStatus.className = 'badge text-bg-secondary';
}
let checkoutInFlight = false;
let checkoutToken = null;

function generateCheckoutToken() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return window.crypto.randomUUID();
  }
  return `chk_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

function ensureCheckoutToken() {
  if (!checkoutToken) {
    checkoutToken = generateCheckoutToken();
  }
  return checkoutToken;
}

function resetCheckoutToken() {
  checkoutToken = null;
}

function getCsrfToken() {
  return document.querySelector('#checkout-form input[name="csrf_token"]')?.value
    || document.querySelector('meta[name="csrf-token"]')?.content
    || '';
}

async function parseApiResponse(response) {
  const contentType = (response.headers.get('content-type') || '').toLowerCase();
  if (contentType.includes('application/json')) {
    const jsonPayload = await response.json();
    return { isJson: true, data: jsonPayload, text: '' };
  }
  const textPayload = await response.text();
  return { isJson: false, data: null, text: (textPayload || '').trim() };
}

function logFetchAudit(label, url, method, csrfPresent) {
  console.info('MP QR fetch outgoing', {
    label,
    url,
    method,
    csrfTokenSent: !!csrfPresent,
  });
}

function logFetchResponseAudit(label, url, method, csrfPresent, response) {
  console.info('MP QR fetch response', {
    label,
    url,
    method,
    csrfTokenSent: !!csrfPresent,
    httpStatus: response?.status ?? null,
    contentType: response?.headers?.get ? response.headers.get('content-type') : null,
  });
}

function getCartTenantKey() {
  const body = document.body;
  const tenantKey = body?.dataset?.cartTenant || '';
  if (tenantKey) return tenantKey;
  const companyId = body?.dataset?.companyId || 'global';
  const userId = body?.dataset?.userId || 'anonymous';
  return `${companyId}:${userId}`;
}

function getCartStorageKey() {
  return `${CART_KEY_PREFIX}_${getCartTenantKey()}`;
}

/**
 * Cargar carrito desde localStorage al iniciar
 */
function loadCart() {
  const savedCart = localStorage.getItem(getCartStorageKey());
  if (localStorage.getItem(CART_KEY_PREFIX)) {
    localStorage.removeItem(CART_KEY_PREFIX);
  }
  if (savedCart) {
    try {
      const parsed = JSON.parse(savedCart);
      cart = Array.isArray(parsed)
        ? parsed.filter(item => {
            const qty = parseFloat(item?.quantity || 0);
            const stock = parseFloat(item?.stock || 0);
            const price = parseFloat(item?.price || 0);
            return Number.isFinite(qty) && qty > 0 && Number.isFinite(stock) && stock > 0 && Number.isFinite(price) && price >= 0 && item?.productId;
          }).map(item => ({
            ...item,
            quantity: Math.min(parseFloat(item.quantity), parseFloat(item.stock)),
          }))
        : [];
    } catch(e) {
      console.warn('Error cargando carrito:', e);
      cart = [];
    }
  }
  updateCartUI();
}

/**
 * Guardar carrito en localStorage
 */
function saveCart() {
  try {
    localStorage.setItem(getCartStorageKey(), JSON.stringify(cart));
    updateCartUI();
  } catch (error) {
    console.error('Excepcion en saveCart():', error);
    throw error;
  }
}

/**
 * Agregar producto al carrito
 * @param {number} productId - ID del producto
 * @param {string} name - Nombre del producto
 * @param {number} price - Precio unitario
 * @param {number} stock - Stock disponible
 * @param {string} barcode - Código de barras (EAN13 o UPC)
 */
function addToCart(productId, name, price, stock, barcode = '', quantity = 1, unitMeasure = 'u') {
  try {
    const qty = Math.max(parseFloat(quantity) || 1, 0.001);
    const availableStock = parseFloat(stock) || 0;
    if (availableStock <= 0 || qty <= 0) {
      showNotification('Stock insuficiente para agregar este producto', 'warning');
      return;
    }
    // Buscar si el producto ya existe en el carrito
    const existingItem = cart.find(item => item.productId === productId);

    if (existingItem) {
      existingItem.quantity = Math.min((parseFloat(existingItem.quantity) || 0) + qty, availableStock);
    } else {
      // Agregar nuevo producto al carrito
      const newItem = {
        productId: productId,
        name: name,
        price: parseFloat(price),
        stock: availableStock,
        barcode: barcode,
        unitMeasure: unitMeasure || 'u',
        quantity: Math.min(qty, availableStock)
      };
      cart.push(newItem);
    }

    saveCart();

    showNotification(name + ' agregado al carrito', 'success');
  } catch (error) {
    console.error('Excepcion en addToCart():', error);
    throw error;
  }
}

/**
 * Eliminar producto del carrito
 * @param {number} productId - ID del producto a eliminar
 */
function removeFromCart(productId) {
  cart = cart.filter(item => item.productId !== productId);
  saveCart();
}

/**
 * Cambiar cantidad de producto en el carrito
 * @param {number} productId - ID del producto
 * @param {number} newQuantity - Nueva cantidad
 */
function updateQuantity(productId, newQuantity) {
  const item = cart.find(item => item.productId === productId);
  const parsedQty = parseFloat(newQuantity);
  if (item && Number.isFinite(parsedQty) && parsedQty > 0) {
    item.quantity = Math.min(parsedQty, item.stock);
  } else {
    // Si la cantidad es <= 0, eliminar el producto
    removeFromCart(productId);
  }
  saveCart();
}

/**
 * Limpiar todo el carrito
 */
function clearCart() {
  cart = [];
  resetCheckoutToken();
  saveCart();
  renderCartModal();
}

/**
 * Actualizar la UI del carrito (contador, resumen, etc.)
 */
function updateCartUI() {
  try {
    const cartBtn = document.querySelector('.cart-float-btn');
    document.querySelectorAll('.cart-count').forEach(el => {
      el.textContent = getCartItemCount();
    });
    renderCartSidebar();
    updateCheckoutTotals();

    if (cart.length > 0) {
      if (cartBtn) {
        cartBtn.classList.remove('d-none');
        cartBtn.querySelector('.cart-count').textContent = getCartItemCount();
      }
    } else {
      if (cartBtn) {
        cartBtn.classList.add('d-none');
      }
    }
  } catch (error) {
    console.error('Excepcion en updateCartUI():', error);
    throw error;
  }
}

function renderCartModal() {
  const container = document.getElementById('cart-items-container');
  if (!container) return;

  if (cart.length === 0) {
    container.innerHTML = '<p class="text-muted mb-0">El carrito esta vacio.</p>';
  } else {
    container.innerHTML = cart.map(item => {
      const productId = parseInt(item.productId, 10) || 0;
      const quantity = parseFloat(item.quantity) || 0;
      const stock = parseFloat(item.stock) || 0;
      return `
        <div class="pos-cart-modal-line">
          <div class="pos-cart-modal-info">
            <strong class="d-block text-truncate">${escapeHtml(item.name)}</strong>
          <small class="text-muted">${formatPrice(item.price)} x ${formatQuantity(quantity)} ${escapeHtml(item.unitMeasure || 'u')}</small>
        </div>
          <div class="pos-cart-modal-controls">
          <button class="btn btn-sm btn-outline-secondary" onclick="updateQuantity(${productId}, ${quantity - 1}); renderCartModal();">-</button>
            <input class="form-control form-control-sm text-center pos-cart-modal-qty-input" type="number" min="0.001" step="0.001" value="${quantity}" onchange="updateQuantity(${productId}, this.value); renderCartModal();">
          <button class="btn btn-sm btn-outline-secondary" onclick="updateQuantity(${productId}, ${quantity + 1}); renderCartModal();" ${quantity >= stock ? 'disabled' : ''}>+</button>
          <button class="btn btn-sm btn-outline-danger" onclick="removeFromCart(${productId}); renderCartModal();">Eliminar</button>
        </div>
      </div>
    `;
    }).join('');
  }

  updateCheckoutTotals();
}

function renderCartSidebar() {
  try {
    const container = document.getElementById('pos-cart-items');
    if (!container) return;

    if (cart.length === 0) {
      container.innerHTML = '<div class="empty-state py-4"><i class="bi bi-cart3 display-6 mb-2"></i><span>El carrito esta vacio.</span></div>';
      return;
    }

    const html = cart.map(item => {
    const productId = parseInt(item.productId, 10) || 0;
    const quantity = parseFloat(item.quantity) || 0;
    const stock = parseFloat(item.stock) || 0;
    return `
    <div class="pos-cart-line">
      <div class="min-w-0">
        <strong class="d-block text-truncate">${escapeHtml(item.name)}</strong>
        <small class="text-muted">${formatPrice(item.price)} x ${formatQuantity(quantity)} ${escapeHtml(item.unitMeasure || 'u')}</small>
      </div>
      <div class="d-flex align-items-center gap-1">
        <button class="btn btn-sm btn-outline-secondary" type="button" onclick="updateQuantity(${productId}, ${quantity - 1})" aria-label="Restar">-</button>
        <span class="small fw-bold px-1">${formatQuantity(quantity)}</span>
        <button class="btn btn-sm btn-outline-secondary" type="button" onclick="updateQuantity(${productId}, ${quantity + 1})" ${quantity >= stock ? 'disabled' : ''} aria-label="Sumar">+</button>
      </div>
    </div>
  `;
    }).join('');
    container.innerHTML = html;
  } catch (error) {
    console.error('Excepcion en renderCartSidebar():', error);
    throw error;
  }
}

function openCartModal() {
  renderCartModal();
  const modal = new bootstrap.Modal(document.getElementById('cartModal'));
  modal.show();
}

async function processCheckout() {
  if (checkoutInFlight) {
    return;
  }
  if (cart.length === 0) {
    showNotification('El carrito esta vacio', 'danger');
    return;
  }

  checkoutInFlight = true;
  if (checkoutProcessButton) {
    checkoutProcessButton.disabled = true;
  }

  if (!validateCheckoutClientRequirements()) {
    checkoutInFlight = false;
    if (checkoutProcessButton) {
      checkoutProcessButton.disabled = false;
    }
    return;
  }

  const selectedMethod = document.getElementById('checkout-payment-method')?.value || '';
  if (selectedMethod === 'QR Mercado Pago') {
    if (mpQrDraftState.approved && mpQrDraftState.finalizeUrl && mpQrDraftState.paymentId) {
      await finalizeMercadoPagoQrSale();
      checkoutInFlight = false;
      return;
    }
    await processMercadoPagoQrCheckout();
    checkoutInFlight = false;
    return;
  }

  const csrf = getCsrfToken();
  const discount = getDiscountBreakdown(getCartSubtotal());
  const payload = {
    items: getCartForCheckout(),
    client_id: document.getElementById('checkout-client-select')?.value || '',
    metodo_pago: document.getElementById('checkout-payment-method')?.value || '',
    metodo_pago_2: document.getElementById('checkout-payment-method-2')?.value || '',
    monto_pago: document.getElementById('checkout-paid-amount')?.value || '',
    monto_pago_2: document.getElementById('checkout-paid-amount-2')?.value || '',
    descuento_general: discount.amount,
    recargo: document.getElementById('checkout-surcharge')?.value || '',
    document_type: document.getElementById('checkout-document-type')?.value || 'venta',
    note: document.getElementById('checkout-note')?.value || '',
    checkout_token: ensureCheckoutToken()
  };
  console.info('[sales] carrito recibido (frontend):', payload);

  try {
    const response = await fetch('/ventas/api/checkout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf, 'X-Cart-Tenant': getCartTenantKey() },
      body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!response.ok) {
      console.error('[sales] error de backend al procesar venta:', { status: response.status, data });
      showNotification(data.error || 'No se pudo procesar la venta', 'danger');
      return;
    }
    console.info('[sales] venta procesada correctamente:', data);
    clearCart();
    window.location.href = data.redirect_url;
  } catch (error) {
    console.error('[sales] excepcion en processCheckout():', error);
    showNotification('No se pudo conectar con el servidor', 'danger');
  } finally {
    checkoutInFlight = false;
    if (checkoutProcessButton) {
      checkoutProcessButton.disabled = false;
      checkoutProcessButton.textContent = 'Procesar venta';
    }
  }
}

function readMoneyInput(id) {
  return parseFloat(document.getElementById(id)?.value || 0) || 0;
}

function getCheckoutTotals() {
  const subtotal = getCartSubtotal();
  const discount = getDiscountBreakdown(subtotal).amount;
  const surcharge = readMoneyInput('checkout-surcharge');
  const taxable = Math.max(subtotal - discount, 0);
  const total = taxable + surcharge;
  const paid = readMoneyInput('checkout-paid-amount') + readMoneyInput('checkout-paid-amount-2');
  return { subtotal, discount, surcharge, total, paid, change: Math.max(paid - total, 0) };
}

function getDiscountBreakdown(subtotal) {
  const mode = document.getElementById('checkout-discount-mode')?.value || 'amount';
  const raw = readMoneyInput('checkout-general-discount');
  if (mode === 'percent') {
    const percent = Math.min(Math.max(raw, 0), 100);
    const amount = subtotal * (percent / 100);
    return {
      mode,
      raw: percent,
      amount,
      label: `${percent.toFixed(2)}% (${formatPrice(amount)})`
    };
  }
  const amount = Math.max(raw, 0);
  return {
    mode,
    raw: amount,
    amount,
    label: formatPrice(amount)
  };
}

function requiresIdentifiedClient() {
  const documentType = document.getElementById('checkout-document-type')?.value || 'venta';
  return ['factura_a', 'factura_b', 'factura_c'].includes(documentType);
}

function validateCheckoutClientRequirements() {
  if (!requiresIdentifiedClient()) {
    return true;
  }
  const clientId = document.getElementById('checkout-client-select')?.value || '';
  if (clientId) {
    return true;
  }
  showNotification('Para este comprobante debés seleccionar un cliente (no Consumidor final).', 'warning');
  document.getElementById('checkout-client-select')?.focus();
  return false;
}

function updateCheckoutTotals() {
  const totals = getCheckoutTotals();
  const pairs = {
    'cart-subtotal': totals.subtotal,
    'cart-total': totals.total,
    'pos-subtotal': totals.subtotal,
    'pos-discount': totals.discount,
    'pos-surcharge': totals.surcharge,
    'pos-total': totals.total,
    'checkout-change': totals.change
  };
  Object.entries(pairs).forEach(([id, value]) => {
    const el = document.getElementById(id);
    if (el) el.textContent = formatPrice(value);
  });
  const paidInput = document.getElementById('checkout-paid-amount');
  if (paidInput && !paidInput.value && totals.total > 0) {
    paidInput.placeholder = totals.total.toFixed(2);
  }

  syncPaymentInputsWithMethods(totals.total);
}

function syncPaymentInputsWithMethods(total) {
  const method2 = document.getElementById('checkout-payment-method-2');
  const method1 = document.getElementById('checkout-payment-method');
  const paid1 = document.getElementById('checkout-paid-amount');
  const paid2 = document.getElementById('checkout-paid-amount-2');
  if (!method2 || !paid1 || !paid2) return;

  const showQr = (method1?.value === 'QR Mercado Pago') || (method2.value === 'QR Mercado Pago');
  if (mpQrPanel) {
    mpQrPanel.classList.toggle('d-none', !showQr);
  }

  if (showQr) {
    loadMercadoPagoPosCatalog();
  }

  if (!showQr) {
    resetMpQrPanel('Elegí QR Mercado Pago y presioná Procesar venta para generar el cobro.', total);
  }

  const hasSecondary = Boolean((method2.value || '').trim());
  paid2.disabled = !hasSecondary;

  if (!hasSecondary) {
    paid2.value = '';
    if (!paid1.value && total > 0) {
      paid1.value = total.toFixed(2);
    }
    return;
  }

  if (!paid1.value && !paid2.value && total > 0) {
    const half = total / 2;
    paid1.value = half.toFixed(2);
    paid2.value = (total - half).toFixed(2);
  }
}

function setupCheckoutPaymentBehavior() {
  const method1 = document.getElementById('checkout-payment-method');
  const method2 = document.getElementById('checkout-payment-method-2');
  const paid1 = document.getElementById('checkout-paid-amount');
  const paid2 = document.getElementById('checkout-paid-amount-2');
  if (!method1 || !method2 || !paid1 || !paid2) return;

  const refresh = () => updateCheckoutTotals();
  method1.addEventListener('change', refresh);
  method2.addEventListener('change', refresh);
  paid1.addEventListener('input', refresh);
  paid2.addEventListener('input', refresh);
  document.getElementById('checkout-client-select')?.addEventListener('change', refresh);
  document.getElementById('checkout-document-type')?.addEventListener('change', refresh);
  document.getElementById('checkout-discount-mode')?.addEventListener('change', () => {
    const mode = document.getElementById('checkout-discount-mode')?.value || 'amount';
    const label = document.getElementById('checkout-discount-value-label');
    if (label) {
      label.textContent = mode === 'percent' ? 'Valor (%)' : 'Valor descuento';
    }
    updateCheckoutTotals();
  });
  refresh();
}

function resetMpQrPanel(message, totalOverride) {
  if (mpQrDraftState.pollTimer) {
    clearInterval(mpQrDraftState.pollTimer);
  }
  mpQrDraftState = {
    paymentId: null,
    statusUrl: null,
    finalizeUrl: null,
    checkoutUrl: null,
    qrDataUri: null,
    pollTimer: null,
    approved: false,
  };
  if (mpQrPanelTitle) mpQrPanelTitle.textContent = 'Esperando generación del QR';
  if (mpQrPanelBadge) {
    mpQrPanelBadge.textContent = 'Pendiente';
    mpQrPanelBadge.className = 'badge text-bg-warning';
  }
  if (mpQrPanelAmount) mpQrPanelAmount.textContent = `Total a cobrar: ${formatPrice(totalOverride ?? getCheckoutTotals().total)}`;
  if (mpQrPanelStatus) mpQrPanelStatus.textContent = message || 'Elegí QR Mercado Pago y presioná Procesar venta para generar el cobro.';
  if (mpQrPanelImage) {
    mpQrPanelImage.src = '';
    mpQrPanelImage.classList.add('d-none');
  }
  if (mpQrModalImage) {
    mpQrModalImage.src = '';
    mpQrModalImage.classList.add('d-none');
  }
  if (mpQrModalStatus) mpQrModalStatus.textContent = message || 'Generá primero el cobro QR desde el carrito.';
  if (mpQrPanelOpen) {
    mpQrPanelOpen.classList.add('d-none');
    mpQrPanelOpen.href = '#';
  }
  if (mpQrPanelOperation) mpQrPanelOperation.textContent = '';
  if (mpQrPanelDateTime) mpQrPanelDateTime.textContent = '';
  if (checkoutProcessButton) {
    checkoutProcessButton.disabled = false;
    checkoutProcessButton.textContent = 'Procesar venta';
  }
}

function cancelMercadoPagoQrCheckout() {
  const total = getCheckoutTotals();
  resetMpQrPanel('Cobro QR cancelado. Podés generar uno nuevo cuando quieras.', total.total);
  showNotification('Cobro QR cancelado.', 'info');
}

function renderMercadoPagoPosOptions(points) {
  if (!mpQrPosSelect) return;
  const currentValue = mpQrPosSelect.value || '';
  mpQrPosSelect.innerHTML = '';

  const defaultOption = document.createElement('option');
  defaultOption.value = '';
  defaultOption.textContent = points.length ? 'Seleccioná un QR/POS...' : 'No hay QR/POS disponibles';
  mpQrPosSelect.appendChild(defaultOption);

  points.forEach((point) => {
    const option = document.createElement('option');
    option.value = String(point.id || '').trim();
    option.dataset.name = String(point.name || point.id || '').trim();
    option.dataset.status = normalizePosStatus(point.status);
    option.dataset.storeName = String(point.store_name || '').trim();
    const storeName = String(point.store_name || '').trim();
    option.textContent = storeName
      ? `${point.name || point.id} (${storeName})`
      : `${point.name || point.id}`;
    mpQrPosSelect.appendChild(option);
  });

  if (currentValue && points.some((point) => String(point.id || '').trim() === currentValue)) {
    mpQrPosSelect.value = currentValue;
  }
  updateSelectedPosBadge();
}

async function loadMercadoPagoPosCatalog(force = false) {
  if (!mpQrPosSelect) {
    console.warn('MP POS catalog load skipped: selector checkout-mp-pos-id no existe en el DOM.');
    return;
  }
  if (!navigator.onLine) {
    renderMercadoPagoPosOptions([]);
    mpPosCatalogLoaded = false;
    if (mpQrPosHelp) {
      mpQrPosHelp.textContent = 'Mercado Pago requiere conexión. Seleccioná otro medio de cobro mientras estás offline.';
    }
    return;
  }
  if (!force && (mpPosCatalogLoaded || mpPosCatalogLoading)) return;

  mpPosCatalogLoading = true;
  mpQrPosSelect.disabled = true;
  mpQrPosSelect.innerHTML = '<option value="">Cargando QR/POS disponibles...</option>';
  if (mpQrPosHelp) {
    mpQrPosHelp.textContent = 'Consultando tu catálogo de QR/POS en Mercado Pago...';
  }

  try {
    const pointsEndpoint = '/ventas/api/mp-qr/points';
    const pointsMethod = 'GET';
    const pointsCsrf = getCsrfToken();
    logFetchAudit('mp-qr-points', pointsEndpoint, pointsMethod, pointsCsrf);
    const response = await fetch(pointsEndpoint, {
      headers: {
        'Accept': 'application/json',
        'X-CSRFToken': pointsCsrf,
      },
    });
    logFetchResponseAudit('mp-qr-points', pointsEndpoint, pointsMethod, pointsCsrf, response);
    const parsed = await parseApiResponse(response);
    if (!parsed.isJson) {
      throw new Error(parsed.text || `Respuesta no JSON (${response.status}) al consultar POS.`);
    }
    const result = parsed.data || {};
    console.info('MP POS catalog response', {
      url: pointsEndpoint,
      httpStatus: response.status,
      json: result,
      count: Number(result.count || 0),
      collector_id: result?.audit?.collector_id || null,
      user_id: result?.audit?.user_id || null,
      company_id: result?.audit?.company_id || null,
      access_token: result?.audit?.access_token || null,
    });
    if (!response.ok) {
      throw new Error(result.error || 'No se pudo consultar los QR/POS de Mercado Pago.');
    }

    const points = Array.isArray(result.points) ? result.points.filter((row) => row && row.id) : [];
    renderMercadoPagoPosOptions(points);
    mpPosCatalogLoaded = true;
    if (mpQrPosHelp) {
      mpQrPosHelp.textContent = points.length
        ? `Se encontraron ${points.length} QR/POS en tu cuenta conectada.`
        : (result.zero_reason || 'No hay QR/POS disponibles en esta cuenta. Verificá la configuración en Mercado Pago.');
    }
    if (!points.length) {
      console.warn('MP POS catalog vacío', {
        zero_reason: result.zero_reason || null,
        mp_http_status: result?.audit?.mp_http_status || null,
        mp_json: result?.audit?.mp_json || null,
      });
    }
  } catch (error) {
    renderMercadoPagoPosOptions([]);
    if (mpQrPosHelp) {
      mpQrPosHelp.textContent = error.message || 'No se pudo consultar los QR/POS de Mercado Pago.';
    }
    console.error('Error cargando catálogo MP POS:', error);
  } finally {
    mpPosCatalogLoading = false;
    mpQrPosSelect.disabled = false;
    updateSelectedPosBadge();
  }
}

function updateMpQrPanel(payload) {
  mpQrDraftState.paymentId = payload.payment_id || mpQrDraftState.paymentId;
  mpQrDraftState.statusUrl = payload.status_url || mpQrDraftState.statusUrl;
  mpQrDraftState.finalizeUrl = payload.finalize_url || mpQrDraftState.finalizeUrl;
  mpQrDraftState.checkoutUrl = payload.checkout_url || mpQrDraftState.checkoutUrl;
  mpQrDraftState.qrDataUri = payload.qr_data_uri || mpQrDraftState.qrDataUri;
  mpQrDraftState.approved = payload.can_process_sale === true || payload.status === 'approved';

  if (mpQrPanelTitle) mpQrPanelTitle.textContent = 'Cobro generado con Mercado Pago';
  if (mpQrPanelBadge) {
    mpQrPanelBadge.textContent = payload.status_label || 'Pendiente';
    mpQrPanelBadge.className = mpQrDraftState.approved ? 'badge text-bg-success' : 'badge text-bg-warning';
  }
  if (mpQrPanelAmount) mpQrPanelAmount.textContent = `Total a cobrar: ${formatPrice(Number(payload.total || getCheckoutTotals().total))}`;
  if (mpQrPanelStatus) mpQrPanelStatus.textContent = payload.status_label || 'Pago pendiente de aprobación.';
  if (mpQrPanelImage && mpQrDraftState.qrDataUri) {
    mpQrPanelImage.src = mpQrDraftState.qrDataUri;
    mpQrPanelImage.classList.remove('d-none');
  }
  if (mpQrModalImage && mpQrDraftState.qrDataUri) {
    mpQrModalImage.src = mpQrDraftState.qrDataUri;
    mpQrModalImage.classList.remove('d-none');
  }
  if (mpQrModalStatus) {
    mpQrModalStatus.textContent = mpQrDraftState.approved ? 'Pago aprobado. Podés finalizar la venta.' : 'Escaneá el QR o abrí Mercado Pago para pagar.';
  }
  if (mpQrPanelOpen && mpQrDraftState.checkoutUrl) {
    mpQrPanelOpen.href = mpQrDraftState.checkoutUrl;
    mpQrPanelOpen.classList.remove('d-none');
  }
  if (mpQrPanelOperation) mpQrPanelOperation.textContent = payload.operation_number ? `Operación: ${payload.operation_number}` : '';
  if (mpQrPanelDateTime) mpQrPanelDateTime.textContent = payload.approved_at ? `Aprobado: ${payload.approved_at}` : '';
  if (checkoutProcessButton) {
    checkoutProcessButton.disabled = !mpQrDraftState.approved;
    checkoutProcessButton.textContent = mpQrDraftState.approved ? 'Finalizar venta' : 'Esperando pago';
  }
}

async function pollMpQrStatus() {
  if (!mpQrDraftState.statusUrl) return;
  try {
    const statusUrl = mpQrDraftState.statusUrl;
    const statusMethod = 'GET';
    const statusCsrf = getCsrfToken();
    logFetchAudit('mp-qr-status', statusUrl, statusMethod, statusCsrf);
    const response = await fetch(statusUrl, { headers: { 'Accept': 'application/json', 'X-CSRFToken': statusCsrf } });
    logFetchResponseAudit('mp-qr-status', statusUrl, statusMethod, statusCsrf, response);
    if (!response.ok) return;
    const payload = await response.json();
    updateMpQrPanel(payload);
    if (payload.can_process_sale && mpQrDraftState.pollTimer) {
      clearInterval(mpQrDraftState.pollTimer);
      mpQrDraftState.pollTimer = null;
    }
  } catch (error) {
    console.error('Error consultando estado de QR MP:', error);
  }
}

async function processMercadoPagoQrCheckout() {
  const total = getCheckoutTotals();
  if (!validateCheckoutClientRequirements()) {
    return;
  }
  if (!navigator.onLine) {
    resetMpQrPanel('Mercado Pago no está disponible sin conexión. Usá efectivo, transferencia u otro medio manual.', total.total);
    showNotification('Mercado Pago no está disponible sin conexión. Usá un cobro manual.', 'warning');
    if (checkoutProcessButton) {
      checkoutProcessButton.disabled = false;
      checkoutProcessButton.textContent = 'Procesar venta';
    }
    return;
  }
  const csrf = getCsrfToken();
  const payload = {
    items: getCartForCheckout(),
    general_discount: total.discount,
    surcharge: total.surcharge,
    client_id: document.getElementById('checkout-client-select')?.value || '',
    mp_pos_id: mpQrPosSelect?.value || '',
    note: document.getElementById('checkout-note')?.value || '',
    document_type: document.getElementById('checkout-document-type')?.value || '',
    checkout_token: ensureCheckoutToken(),
  };

  if (mpQrPosSelect && mpQrPosSelect.options.length > 1 && !payload.mp_pos_id) {
    showNotification('Seleccioná un QR/POS de Mercado Pago antes de generar el cobro.', 'warning');
    mpQrPosSelect.focus();
    if (checkoutProcessButton && !mpQrDraftState.approved) {
      checkoutProcessButton.disabled = false;
      checkoutProcessButton.textContent = 'Procesar venta';
    }
    return;
  }

  const selectedPosMeta = getSelectedPosMeta();
  if (selectedPosMeta && selectedPosMeta.status && selectedPosMeta.status !== 'active') {
    showNotification('El QR/POS seleccionado no está activo en Mercado Pago. Elegí uno activo.', 'warning');
    if (checkoutProcessButton && !mpQrDraftState.approved) {
      checkoutProcessButton.disabled = false;
      checkoutProcessButton.textContent = 'Procesar venta';
    }
    return;
  }
  if (checkoutProcessButton) {
    checkoutProcessButton.disabled = true;
    checkoutProcessButton.textContent = 'Generando QR...';
  }

  try {
    const createUrl = '/ventas/api/mp-qr/create';
    const createMethod = 'POST';
    const createCsrf = csrf;
    logFetchAudit('mp-qr-create', createUrl, createMethod, createCsrf);
    const response = await fetch('/ventas/api/mp-qr/create', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': createCsrf,
        'X-Cart-Tenant': getCartTenantKey(),
      },
      body: JSON.stringify(payload),
    });
    logFetchResponseAudit('mp-qr-create', createUrl, createMethod, createCsrf, response);
    const parsed = await parseApiResponse(response);
    if (!parsed.isJson) {
      const fallbackText = parsed.text || `Error HTTP ${response.status} al generar el cobro.`;
      throw new Error(fallbackText);
    }
    const result = parsed.data || {};
    if (result && result.mp_pos_audit) {
      console.info('MP POS audit', result.mp_pos_audit);
    }
    if (!response.ok) {
      throw new Error(result.error || `Error HTTP ${response.status} al generar el cobro.`);
    }
    updateMpQrPanel(result);
    if (mpQrDraftState.pollTimer) clearInterval(mpQrDraftState.pollTimer);
    mpQrDraftState.pollTimer = window.setInterval(pollMpQrStatus, 3000);
    await pollMpQrStatus();
    showNotification('QR generado. Esperando aprobación del pago.', 'info');
  } catch (error) {
    resetMpQrPanel(error.message || 'No se pudo generar el cobro.', total.total);
    showNotification(error.message || 'No se pudo generar el cobro.', 'danger');
  } finally {
    if (checkoutProcessButton && !mpQrDraftState.approved) {
      checkoutProcessButton.disabled = false;
      checkoutProcessButton.textContent = 'Procesar venta';
    }
  }
}

async function finalizeMercadoPagoQrSale() {
  if (!mpQrDraftState.finalizeUrl || !mpQrDraftState.paymentId) return;
  if (!navigator.onLine) {
    showNotification('No se puede finalizar una venta de Mercado Pago sin conexión.', 'warning');
    return;
  }
  const csrf = getCsrfToken();
  if (checkoutProcessButton) {
    checkoutProcessButton.disabled = true;
    checkoutProcessButton.textContent = 'Finalizando venta...';
  }
  try {
    const finalizeUrl = mpQrDraftState.finalizeUrl;
    const finalizeMethod = 'POST';
    const finalizeCsrf = csrf;
    logFetchAudit('mp-qr-finalize', finalizeUrl, finalizeMethod, finalizeCsrf);
    const response = await fetch(mpQrDraftState.finalizeUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrf,
        'X-Cart-Tenant': getCartTenantKey(),
      },
      body: JSON.stringify({ draft_id: mpQrDraftState.paymentId }),
    });
    logFetchResponseAudit('mp-qr-finalize', finalizeUrl, finalizeMethod, finalizeCsrf, response);
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.error || 'No se pudo finalizar la venta.');
    }
    clearCart();
    resetCheckoutToken();
    resetMpQrPanel('Venta finalizada correctamente.');
    if (result.redirect_url) {
      window.location.href = result.redirect_url;
    }
  } catch (error) {
    showNotification(error.message || 'No se pudo finalizar la venta.', 'danger');
    if (checkoutProcessButton) {
      checkoutProcessButton.disabled = false;
      checkoutProcessButton.textContent = 'Finalizar venta';
    }
  }
}

/**
 * Obtener cantidad total de items en el carrito
 */
function getCartItemCount() {
  return formatQuantity(cart.reduce((sum, item) => sum + (parseFloat(item.quantity) || 0), 0));
}

/**
 * Preparar datos del carrito para el checkout
 * @returns {Array} Array de objetos con productId, name, price, quantity, barcode
 */
function getCartForCheckout() {
  return cart.map(item => ({
    productId: item.productId,
    name: item.name,
    price: item.price,
    quantity: item.quantity,
    barcode: item.barcode || ''
  }));
}

/**
 * Obtener subtotal del carrito
 */
function getCartSubtotal() {
  return cart.reduce((sum, item) => sum + ((parseFloat(item.price) || 0) * (parseFloat(item.quantity) || 0)), 0);
}

/**
 * Formatear precio a formato moneda
 * @param {number} amount - Monto a formatear
 */
function formatPrice(amount) {
  return '$' + (parseFloat(amount) || 0).toFixed(2);
}

function formatQuantity(quantity) {
  const value = parseFloat(quantity) || 0;
  return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}

function escapeHtml(value) {
  const div = document.createElement('div');
  div.textContent = value ?? '';
  return div.innerHTML;
}

/**
 * Mostrar notificación toast
 * @param {string} message - Mensaje a mostrar
 * @param {string} type - 'success', 'error', o 'info'
 */
function showNotification(message, type = 'success') {
  // Crear contenedor del toast si no existe
  let toastContainer = document.querySelector('.toast-container');
  if (!toastContainer) {
    toastContainer = document.createElement('div');
    toastContainer.className = 'toast-container position-fixed top-0 end-0 p-3';
    document.body.appendChild(toastContainer);
  }
  
  // Crear el toast
  const toastId = 'notification-' + Date.now();
  const toastEl = document.createElement('div');
  toastEl.id = toastId;
  const bootstrapType = type === 'error' ? 'danger' : type;
  const safeType = ['success', 'danger', 'warning', 'info', 'primary', 'secondary'].includes(bootstrapType) ? bootstrapType : 'info';
  toastEl.className = `toast align-items-center text-bg-${safeType} border-0`;
  toastEl.setAttribute('role', 'alert');
  
  toastEl.innerHTML = `
    <div class="d-flex">
      <div class="toast-body">
        ${escapeHtml(message)}
      </div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
    </div>
  `;
  
  toastContainer.appendChild(toastEl);
  const toast = new bootstrap.Toast(toastEl, { delay: 3000 });
  toast.show();
}

/**
 * Inicializar cart-manager cuando la página carga
 */
document.addEventListener('DOMContentLoaded', () => {
  loadCart();
  setupFastScanner();
  setupCheckoutPaymentBehavior();
  ['checkout-general-discount', 'checkout-surcharge', 'checkout-paid-amount', 'checkout-paid-amount-2'].forEach(id => {
    document.getElementById(id)?.addEventListener('input', updateCheckoutTotals);
  });
  window.addEventListener('beforeunload', () => {
    if (mpQrDraftState.pollTimer) {
      clearInterval(mpQrDraftState.pollTimer);
    }
  });
  mpQrPosSelect?.addEventListener('change', updateSelectedPosBadge);
  updateSelectedPosBadge();
});

async function createQuickClient() {
  const nameInput = document.getElementById('quick-client-name');
  const emailInput = document.getElementById('quick-client-email');
  const whatsappInput = document.getElementById('quick-client-whatsapp');
  const actionButton = document.getElementById('quick-client-create-btn');
  const name = (nameInput?.value || '').trim();

  if (!name) {
    showNotification('Ingresá el nombre del cliente.', 'warning');
    nameInput?.focus();
    return;
  }

  if (actionButton) {
    actionButton.disabled = true;
    actionButton.textContent = 'Creando...';
  }

  try {
    const response = await fetch('/clientes/api/quick-create', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': getCsrfToken(),
      },
      body: JSON.stringify({
        name,
        email: (emailInput?.value || '').trim(),
        whatsapp: (whatsappInput?.value || '').trim(),
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || 'No se pudo crear el cliente.');
    }

    const client = data.client || {};
    const select = document.getElementById('checkout-client-select');
    if (select && client.id) {
      const newOption = document.createElement('option');
      newOption.value = String(client.id);
      newOption.textContent = client.name || `Cliente #${client.id}`;
      select.appendChild(newOption);
      select.value = String(client.id);
      select.dispatchEvent(new Event('change'));
    }

    if (nameInput) nameInput.value = '';
    if (emailInput) emailInput.value = '';
    if (whatsappInput) whatsappInput.value = '';

    bootstrap.Modal.getInstance(document.getElementById('quickClientModal'))?.hide();
    showNotification('Cliente creado y seleccionado.', 'success');
  } catch (error) {
    showNotification(error.message || 'No se pudo crear el cliente.', 'danger');
  } finally {
    if (actionButton) {
      actionButton.disabled = false;
      actionButton.textContent = 'Crear cliente';
    }
  }
}

function setupFastScanner() {
  const input = document.getElementById('fast-scanner-input');
  if (!input) return;
  let scanTimer = null;
  input.focus();
  document.addEventListener('click', () => {
    const activeTag = document.activeElement?.tagName;
    const cartModalOpen = document.getElementById('cartModal')?.classList.contains('show');
    const isEditableElement = ['INPUT', 'TEXTAREA', 'SELECT', 'OPTION', 'BUTTON'].includes(activeTag);
    if (!cartModalOpen && !isEditableElement) {
      input.focus();
    }
  });
  input.addEventListener('input', () => {
    clearTimeout(scanTimer);
    scanTimer = setTimeout(() => processScan(input.value.trim()), 120);
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      clearTimeout(scanTimer);
      processScan(input.value.trim());
    }
  });
}

async function startCameraScanner() {
  const video = document.getElementById('camera-scanner-video');
  const container = document.getElementById('camera-scanner-container');
  const status = document.getElementById('camera-scanner-status');
  if (!video && !container) return;
  const useNativeDetector = 'BarcodeDetector' in window && navigator.mediaDevices?.getUserMedia;
  try {
    stopCameraScanner();
    if (useNativeDetector && video) {
      video.classList.remove('d-none');
      if (container) container.classList.add('d-none');
      scannerStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
      video.srcObject = scannerStream;
      await video.play();
      scannerLoopActive = true;
      const detector = new BarcodeDetector({ formats: ['qr_code', 'ean_13', 'code_128'] });
      if (status) status.textContent = 'Apunta al QR o código del producto.';
      scanCameraFrame(detector, video, status);
      return;
    }

    if (typeof Html5Qrcode === 'undefined') {
      if (status) status.textContent = 'Este navegador no soporta cámara nativa. Usa lector Bluetooth/USB o el campo de código.';
      return;
    }

    if (container) {
      if (video) video.classList.add('d-none');
      container.classList.remove('d-none');
      container.innerHTML = '<div id="html5qr-reader" style="width:100%"></div>';
      html5QrScanner = new Html5Qrcode('html5qr-reader');
      if (status) status.textContent = 'Abriendo cámara para leer QR del producto...';
      await html5QrScanner.start(
        { facingMode: 'environment' },
        { fps: 12, qrbox: { width: 240, height: 240 }, aspectRatio: 1 },
        async decodedText => {
          if (!scannerLoopActive) return;
          if (status) status.textContent = 'Código detectado: ' + decodedText;
          await processScan(decodedText);
          stopCameraScanner();
          const modal = bootstrap.Modal.getInstance(document.getElementById('cameraScannerModal'));
          modal?.hide();
        },
        () => {
          if (status) status.textContent = 'Esperando código...';
        }
      );
      scannerLoopActive = true;
    }
  } catch (error) {
    if (status) status.textContent = 'No se pudo acceder a la cámara.';
  }
}

async function scanCameraFrame(detector, video, status) {
  if (!scannerLoopActive) return;
  try {
    const codes = await detector.detect(video);
    if (codes.length) {
      const code = codes[0].rawValue;
      if (status) status.textContent = 'Codigo detectado: ' + code;
      await processScan(code);
      stopCameraScanner();
      const modal = bootstrap.Modal.getInstance(document.getElementById('cameraScannerModal'));
      modal?.hide();
      return;
    }
  } catch (error) {
    if (status) status.textContent = 'Esperando codigo...';
  }
  requestAnimationFrame(() => scanCameraFrame(detector, video, status));
}

function stopCameraScanner() {
  scannerLoopActive = false;
  if (scannerStream) {
    scannerStream.getTracks().forEach(track => track.stop());
    scannerStream = null;
  }
  if (html5QrScanner) {
    html5QrScanner.stop().catch(() => {}).finally(() => {
      html5QrScanner.clear().catch(() => {});
      html5QrScanner = null;
    });
  }
  const container = document.getElementById('camera-scanner-container');
  if (container) container.innerHTML = '';
  const video = document.getElementById('camera-scanner-video');
  if (video) video.classList.remove('d-none');
}

async function processScan(code) {
  const input = document.getElementById('fast-scanner-input');
  if (!code) return;
  try {
    const response = await fetch('/productos/api/' + encodeURIComponent(code));
    const product = await response.json();
    if (!response.ok) {
      showNotification(product.error || 'Producto no encontrado', 'danger');
      return;
    }
    addToCart(product.id, product.name, product.price, product.stock, product.barcode, 1, product.unit_measure || 'u');
    renderCartModal();
  } catch (error) {
    showNotification('No se pudo leer el codigo escaneado', 'danger');
  } finally {
    if (input) {
      input.value = '';
      input.focus();
    }
  }
}
