/* Live multi-device stock synchronization.
 * The server remains authoritative for every write; this only refreshes
 * visible stock indicators on already-open screens.
 */
(() => {
  const selectors = '[data-live-stock], [data-live-stock-button]';
  if (!document.querySelector(selectors)) return;

  let running = false;
  let timer = null;

  const formatQuantity = (value) => {
    const number = Number(value || 0);
    if (!Number.isFinite(number)) return '0';
    return Number.isInteger(number) ? String(number) : number.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
  };

  const refresh = async () => {
    if (running || document.hidden || !navigator.onLine) return;
    running = true;
    try {
      const response = await fetch('/productos/api/products?_sync=' + Date.now(), {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'Accept': 'application/json', 'Cache-Control': 'no-cache' },
      });
      if (!response.ok) return;
      const payload = await response.json();
      const products = new Map((payload.products || []).map(product => [String(product.id), product]));

      document.querySelectorAll('[data-live-stock]').forEach((element) => {
        const id = String(element.dataset.liveStock || '');
        const product = products.get(id);
        if (!product) return;
        const stock = Number(product.stock || 0);
        const minStock = Number(product.min_stock || 0);
        const unit = product.unit_measure || 'u';
        element.textContent = 'Stock ' + formatQuantity(stock) + (unit ? ' ' + unit : '');
        element.classList.remove('text-bg-danger', 'text-bg-warning', 'text-bg-success', 'bg-danger', 'bg-warning', 'bg-success');
        element.classList.add(stock <= 0 ? 'text-bg-danger' : (stock <= minStock ? 'text-bg-warning' : 'text-bg-success'));
        element.dataset.stockValue = String(stock);
      });

      document.querySelectorAll('[data-live-stock-button]').forEach((button) => {
        const id = String(button.dataset.liveStockButton || '');
        const product = products.get(id);
        if (!product) return;
        const stock = Number(product.stock || 0);
        button.disabled = stock <= 0;
        button.dataset.productStock = String(stock);
      });

      document.querySelectorAll('[data-product-card][data-product-id]').forEach((card) => {
        const product = products.get(String(card.dataset.productId || ''));
        if (!product) return;
        card.dataset.productStock = String(product.stock || 0);
      });
    } catch (_) {
      // Background synchronization is best-effort. Checkout always revalidates
      // the authoritative stock inside a database transaction.
    } finally {
      running = false;
    }
  };

  const start = () => {
    if (timer) clearInterval(timer);
    refresh();
    timer = window.setInterval(refresh, 8000);
  };

  window.addEventListener('online', refresh);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refresh();
  });
  start();
})();
