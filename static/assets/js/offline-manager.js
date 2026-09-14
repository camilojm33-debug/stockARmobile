(function () {
  function installAiOrdersNav() {
    const nav = document.querySelector('.app-nav');
    if (!nav || nav.querySelector('a[href="/pedidos-ia"]')) return;
    const links = Array.from(nav.querySelectorAll('a.nav-link'));
    const companyLink = links.find((link) => (link.textContent || '').trim().toLowerCase().includes('mi empresa'));
    const quoteLink = links.find((link) => (link.textContent || '').trim().toLowerCase().includes('presupuestos'));
    if (!companyLink || !quoteLink) return;
    const link = document.createElement('a');
    link.className = 'nav-link';
    link.href = '/pedidos-ia';
    link.innerHTML = '<i class="bi bi-robot"></i><span class="nav-text">Pedidos IA</span>';
    if (window.location.pathname === '/pedidos-ia' || window.location.pathname.startsWith('/pedidos-ia/')) link.classList.add('active');
    quoteLink.insertAdjacentElement('afterend', link);
  }

  function installPricingControllerNav() {
    const nav = document.querySelector('.app-nav');
    if (!nav || nav.querySelector('a[href="/precios/"]')) return;
    const links = Array.from(nav.querySelectorAll('a.nav-link'));
    const productLink = links.find((link) => (link.getAttribute('href') || '') === '/productos/' || (link.textContent || '').trim().toLowerCase() === 'productos');
    if (!productLink) return;

    const link = document.createElement('a');
    link.className = 'nav-link';
    link.href = '/precios/';
    link.innerHTML = '<i class="bi bi-sliders2"></i><span class="nav-text">Precios globales</span>';
    if (window.location.pathname === '/precios/' || window.location.pathname.startsWith('/precios/')) link.classList.add('active');
    productLink.insertAdjacentElement('afterend', link);
  }

  function installPricingControllerShortcut() {
    if (!window.location.pathname.startsWith('/productos')) return;
    if (document.querySelector('[data-price-controller-shortcut="true"]')) return;

    const toolbar = document.querySelector('.toolbar-card');
    if (!toolbar) return;

    const wrapper = document.createElement('div');
    wrapper.className = 'd-flex justify-content-end mt-2';
    wrapper.dataset.priceControllerShortcut = 'true';
    wrapper.innerHTML = '<a href="/precios/" class="btn btn-outline-primary btn-sm"><i class="bi bi-sliders2 me-1"></i>Controlador global de precios</a>';
    toolbar.appendChild(wrapper);
  }

  function loadOfflineCore() {
    if (document.querySelector('script[data-offline-core="true"]')) return;
    const script = document.createElement('script');
    script.src = '/static/assets/js/offline-manager-core.js?v=20260911-ai-orders';
    script.dataset.offlineCore = 'true';
    script.async = false;
    document.head.appendChild(script);
  }

  function loadInvoiceCamera() {
    if (!document.getElementById('invoice-camera-input') || document.querySelector('script[data-invoice-camera="true"]')) return;
    const script = document.createElement('script');
    script.src = '/static/assets/js/invoice-camera.js?v=20260913';
    script.dataset.invoiceCamera = 'true';
    script.defer = true;
    document.head.appendChild(script);
  }

  installAiOrdersNav();
  installPricingControllerNav();
  installPricingControllerShortcut();
  loadOfflineCore();
  loadInvoiceCamera();
})();
