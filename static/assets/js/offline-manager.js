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

  function loadOfflineCore() {
    if (document.querySelector('script[data-offline-core="true"]')) return;
    const script = document.createElement('script');
    script.src = '/static/assets/js/offline-manager-core.js?v=20260911-ai-orders';
    script.dataset.offlineCore = 'true';
    script.async = false;
    document.head.appendChild(script);
  }

  installAiOrdersNav();
  loadOfflineCore();
})();
