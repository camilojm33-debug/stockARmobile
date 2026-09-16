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

  function installQuickGuideEnhancements() {
    const modal = document.getElementById('quickGuideModal');
    const role = (document.body?.dataset?.role || '').toLowerCase();
    if (!modal || role === 'seller' || modal.dataset.enhanced === 'true') return;

    const dialog = modal.querySelector('.modal-dialog');
    const body = modal.querySelector('.modal-body');
    if (!dialog || !body) return;

    dialog.classList.add('modal-lg', 'modal-dialog-scrollable');
    body.classList.remove('pt-0');
    body.innerHTML = `
      <style>
        #quickGuideModal .guide-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:.75rem; }
        #quickGuideModal .guide-card { border:1px solid var(--app-line,#e2e8f0); border-radius:12px; padding:.8rem .9rem; background:var(--app-panel,#fff); height:100%; }
        #quickGuideModal .guide-card strong { display:block; margin-bottom:.2rem; }
        #quickGuideModal .guide-card p { margin:0; color:var(--app-muted,#64748b); font-size:.82rem; line-height:1.42; }
        #quickGuideModal .guide-section-title { display:flex; align-items:center; gap:.5rem; font-weight:800; margin:1rem 0 .6rem; }
        #quickGuideModal .guide-step { display:flex; gap:.65rem; align-items:flex-start; padding:.55rem .65rem; border-radius:10px; background:rgba(37,99,235,.045); margin-bottom:.45rem; }
        #quickGuideModal .guide-step-num { flex:0 0 28px; width:28px; height:28px; border-radius:50%; display:inline-flex; align-items:center; justify-content:center; background:#2563eb; color:#fff; font-size:.76rem; font-weight:800; }
        #quickGuideModal .guide-actions { display:flex; flex-wrap:wrap; gap:.5rem; }
        #quickGuideModal .guide-actions .btn { min-width:0; flex:1 1 145px; }
        [data-bs-theme="dark"] #quickGuideModal .guide-card { background:var(--app-panel,#162033); }
        [data-bs-theme="dark"] #quickGuideModal .guide-step { background:rgba(37,99,235,.12); }
        @media (max-width: 575.98px) {
          #quickGuideModal .guide-grid { grid-template-columns:1fr; }
          #quickGuideModal .modal-body { padding:.95rem !important; }
          #quickGuideModal .modal-footer { padding:.75rem .95rem; }
          #quickGuideModal .guide-actions .btn { flex-basis:100%; }
        }
      </style>

      <p class="text-muted small mb-3">Ruta de puesta en marcha y operación diaria de StockArmobile.</p>

      <div class="guide-section-title"><i class="bi bi-rocket-takeoff text-primary"></i>Ruta de puesta en marcha</div>
      <div class="mb-3">
        <div class="guide-step"><span class="guide-step-num">1</span><div><strong>Configurá el comercio</strong><div class="small text-muted">Completá los datos de la empresa y los permisos del equipo.</div></div></div>
        <div class="guide-step"><span class="guide-step-num">2</span><div><strong>Cargá productos y stock</strong><div class="small text-muted">Definí precios, existencias y stock mínimo para operar.</div></div></div>
        <div class="guide-step"><span class="guide-step-num">3</span><div><strong>Registrá clientes</strong><div class="small text-muted">Guardá datos e historial para ventas recurrentes.</div></div></div>
        <div class="guide-step"><span class="guide-step-num">4</span><div><strong>Abrí caja y configurá cobros</strong><div class="small text-muted">Verificá medios de pago antes de realizar la primera operación.</div></div></div>
        <div class="guide-step"><span class="guide-step-num">5</span><div><strong>Hacé la primera venta</strong><div class="small text-muted">Agregá productos al carrito, elegí cliente y medio de pago y confirmá.</div></div></div>
      </div>

      <div class="guide-section-title"><i class="bi bi-grid text-primary"></i>Funciones principales</div>
      <div class="guide-grid">
        <div class="guide-card"><strong>📦 Productos y stock</strong><p>Controlá precios, existencias, mínimos, categorías, QR y lectura por scanner.</p></div>
        <div class="guide-card"><strong>👥 Clientes</strong><p>Registralos para historial, crédito y seguimiento de ventas.</p></div>
        <div class="guide-card"><strong>💰 Caja</strong><p>Abrí, registrá movimientos y cerrá la jornada controlando diferencias.</p></div>
        <div class="guide-card"><strong>🛒 Primera venta</strong><p>Usá venta rápida, scanner y los medios de pago disponibles.</p></div>
        <div class="guide-card"><strong>🚚 Compras</strong><p>Registrá ingresos de mercadería para mantener actualizado el stock.</p></div>
        <div class="guide-card"><strong>📊 Reportes</strong><p>Revisá ventas, stock y clientes para detectar cambios y oportunidades.</p></div>
      </div>

      <div class="guide-section-title"><i class="bi bi-file-earmark-text text-primary"></i>Presupuestos, pagos y venta confirmada</div>
      <div class="guide-grid">
        <div class="guide-card"><strong>🧾 Presupuesto pendiente</strong><p>La propuesta todavía no está confirmada.</p></div>
        <div class="guide-card"><strong>✅ Presupuesto aceptado</strong><p>El cliente aceptó la propuesta; esto no significa por sí solo que exista una venta.</p></div>
        <div class="guide-card"><strong>💳 Pago</strong><p>Puede estar pendiente, en proceso o aprobado según el medio y la respuesta recibida.</p></div>
        <div class="guide-card"><strong>🟢 Venta confirmada</strong><p>La operación fue convertida correctamente en una venta registrada.</p></div>
      </div>

      <div class="guide-section-title"><i class="bi bi-robot text-primary"></i>Agentes IA</div>
      <div class="guide-grid">
        <div class="guide-card"><strong>🤖 Vendedor IA 24/7</strong><p>Atiende consultas comerciales, busca productos y puede armar pedidos o presupuestos según el flujo habilitado.</p></div>
        <div class="guide-card"><strong>🧠 Asistente Empresarial</strong><p>Consulta información del negocio y ayuda con ventas, stock, productos y clientes.</p></div>
        <div class="guide-card"><strong>📊 Analista IA</strong><p>Trabaja sobre los datos disponibles para detectar patrones, comparaciones y oportunidades.</p></div>
        <div class="guide-card"><strong>📣 Marketing IA</strong><p>Ayuda con segmentaciones y campañas; las campañas quedan como borradores hasta su revisión.</p></div>
      </div>

      <div class="guide-section-title"><i class="bi bi-credit-card text-primary"></i>Mercado Pago</div>
      <div class="guide-card mb-3">
        <div class="small mb-1"><strong>Pendiente:</strong> todavía no se confirmó el pago.</div>
        <div class="small mb-1"><strong>En proceso:</strong> el proveedor de pagos continúa procesándolo.</div>
        <div class="small mb-1"><strong>Aprobado:</strong> el pago fue aprobado.</div>
        <div class="small"><strong>Venta confirmada:</strong> el pedido fue convertido correctamente en venta.</div>
      </div>

      <div class="guide-section-title"><i class="bi bi-shield-check text-primary"></i>Buenas prácticas de seguridad</div>
      <div class="guide-grid">
        <div class="guide-card"><strong>🔐 Usuarios individuales</strong><p>Cada empleado debe usar su propia cuenta y no compartir contraseñas.</p></div>
        <div class="guide-card"><strong>🛡️ Permisos</strong><p>Revisá los permisos antes de entregar acceso a nuevas funciones.</p></div>
        <div class="guide-card"><strong>🔎 Verificación</strong><p>Antes de cerrar una operación, confirmá el estado real de pago y venta.</p></div>
        <div class="guide-card"><strong>🆘 Incidentes</strong><p>Ante un error, guardá el mensaje, la pantalla y la operación afectada para soporte.</p></div>
      </div>

      <div class="guide-section-title"><i class="bi bi-lightning-charge text-primary"></i>Accesos directos</div>
      <div class="guide-actions" data-guide-actions>
        <a class="btn btn-outline-primary" data-guide-link="productos"><i class="bi bi-box-seam me-1"></i>Productos</a>
        <a class="btn btn-outline-primary" data-guide-link="clientes"><i class="bi bi-people me-1"></i>Clientes</a>
        <a class="btn btn-outline-primary" data-guide-link="caja"><i class="bi bi-cash-coin me-1"></i>Caja</a>
        <a class="btn btn-primary" data-guide-link="ventas"><i class="bi bi-cart-check me-1"></i>Nueva venta</a>
        <a class="btn btn-outline-primary" data-guide-link="presupuestos"><i class="bi bi-file-earmark-text me-1"></i>Presupuestos</a>
        <a class="btn btn-outline-primary" data-guide-link="compras"><i class="bi bi-truck me-1"></i>Compras</a>
        <a class="btn btn-outline-primary" data-guide-link="ia"><i class="bi bi-stars me-1"></i>IA</a>
      </div>
    `;

    const navLinks = Array.from(document.querySelectorAll('.app-nav a.nav-link'));
    const resolveNavHref = (term) => {
      const link = navLinks.find((candidate) => (candidate.textContent || '').trim().toLowerCase().includes(term));
      return link?.getAttribute('href') || '';
    };
    const hrefs = {
      productos: resolveNavHref('productos'),
      clientes: resolveNavHref('clientes'),
      caja: resolveNavHref('caja'),
      ventas: resolveNavHref('ventas'),
      presupuestos: resolveNavHref('presupuestos'),
      compras: resolveNavHref('compras'),
      ia: resolveNavHref('ia para tu negocio') || '/dashboard/ia'
    };
    modal.querySelectorAll('[data-guide-link]').forEach((link) => {
      const key = link.dataset.guideLink;
      const href = hrefs[key];
      if (href) link.href = href;
      else link.classList.add('d-none');
    });

    modal.dataset.enhanced = 'true';
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
  installQuickGuideEnhancements();
  loadOfflineCore();
  loadInvoiceCamera();
})();
