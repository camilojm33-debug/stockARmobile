(function () {
  const CATEGORY_RULES = [
    { icon: '🥤', keys: ['bebida', 'gaseosa', 'jugo', 'agua', 'cerveza', 'vino'] },
    { icon: '🧀', keys: ['lacteo', 'lacteos', 'queso', 'yogur', 'yoghurt', 'leche'] },
    { icon: '🍞', keys: ['pan', 'panificado', 'panificados', 'panaderia', 'factura', 'bolleria'] },
    { icon: '🥩', keys: ['carne', 'carnes', 'pollo', 'cerdo', 'vacuno', 'fiambre'] },
    { icon: '🍎', keys: ['fruta', 'frutas', 'manzana', 'banana', 'citricos'] },
    { icon: '🥦', keys: ['verdura', 'verduras', 'hortaliza', 'hortalizas'] },
    { icon: '🧴', keys: ['limpieza', 'higiene', 'detergente', 'lavandina'] },
    { icon: '🧴', keys: ['perfumeria', 'perfume', 'cosmetica', 'cosmeticos'] },
    { icon: '🔩', keys: ['ferreteria', 'tornillo', 'bulon', 'tuerca'] },
    { icon: '💡', keys: ['electricidad', 'electrico', 'electrica', 'lampara', 'foco'] },
    { icon: '🧱', keys: ['construccion', 'cemento', 'arena', 'ladrillo'] },
    { icon: '🎨', keys: ['pintureria', 'pintura', 'esmalte', 'rodillo'] },
    { icon: '💻', keys: ['electronica', 'electro', 'notebook', 'laptop'] },
    { icon: '🖥️', keys: ['informatica', 'pc', 'computacion', 'monitor'] },
    { icon: '📱', keys: ['telefonia', 'telefono', 'celular', 'smartphone'] },
    { icon: '🎧', keys: ['accesorio', 'accesorios', 'auricular', 'headset'] },
    { icon: '👕', keys: ['indumentaria', 'ropa', 'remera', 'campera'] },
    { icon: '👟', keys: ['calzado', 'zapatilla', 'zapato', 'botin'] },
    { icon: '🐶', keys: ['mascota', 'mascotas', 'veterinaria', 'pet'] },
    { icon: '💊', keys: ['farmacia', 'medicamento', 'medicina', 'salud'] },
    { icon: '🧸', keys: ['juguete', 'juguetes', 'juego', 'didactico'] },
    { icon: '📚', keys: ['libreria', 'libro', 'cuaderno', 'lectura'] },
    { icon: '📝', keys: ['papeleria', 'papel', 'utiles', 'oficina'] },
    { icon: '🚗', keys: ['automotor', 'auto', 'vehiculo', 'moto'] },
    { icon: '⚙️', keys: ['repuesto', 'repuestos', 'autoparte'] },
    { icon: '🛠️', keys: ['herramienta', 'herramientas', 'taladro', 'martillo'] },
    { icon: '📦', keys: ['general', 'otros', 'otro'] },
  ];

  function normalizeText(value) {
    return String(value || '')
      .toLowerCase()
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .trim();
  }

  function escapeHtml(value) {
    return String(value || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function getCategoryIcon(category) {
    const normalized = normalizeText(category);
    if (!normalized) return '📦';

    for (const rule of CATEGORY_RULES) {
      if (rule.keys.some((key) => normalized.includes(key))) {
        return rule.icon;
      }
    }

    return '📦';
  }

  function fallbackAriaLabel(name, category) {
    const safeName = String(name || 'producto').trim() || 'producto';
    const safeCategory = String(category || 'general').trim() || 'general';
    return `Sin imagen de ${safeName}. Categoría ${safeCategory}`;
  }

  function buildFallbackHtml(options) {
    const className = String(options?.className || '');
    const extraClass = String(options?.extraClass || '');
    const category = options?.category || '';
    const name = options?.name || '';
    const icon = getCategoryIcon(category);
    const label = fallbackAriaLabel(name, category);

    return `<div class="${escapeHtml(className)} ${escapeHtml(extraClass)} product-icon-fallback" role="img" aria-label="${escapeHtml(label)}">${icon}</div>`;
  }

  function applyCategoryIconTargets(root) {
    const scope = root || document;
    scope.querySelectorAll('[data-category-icon]').forEach((node) => {
      const category = node.getAttribute('data-category') || '';
      const name = node.getAttribute('data-product-name') || 'producto';
      node.textContent = getCategoryIcon(category);
      if (!node.getAttribute('aria-label')) {
        node.setAttribute('aria-label', fallbackAriaLabel(name, category));
      }
      node.setAttribute('role', 'img');
      node.classList.add('product-icon-fallback');
    });
  }

  function replaceBrokenImage(imgNode) {
    if (!imgNode || !imgNode.parentNode) return;

    const fallbackClass = imgNode.getAttribute('data-fallback-class') || imgNode.className || '';
    const fallbackExtraClass = imgNode.getAttribute('data-fallback-extra-class') || '';
    const category = imgNode.getAttribute('data-category') || '';
    const name = imgNode.getAttribute('data-product-name') || '';

    const html = buildFallbackHtml({
      className: fallbackClass,
      extraClass: fallbackExtraClass,
      category,
      name,
    });

    imgNode.insertAdjacentHTML('afterend', html);
    imgNode.remove();
  }

  function attachImageFallbacks(root) {
    const scope = root || document;
    scope.querySelectorAll('[data-product-image]').forEach((imgNode) => {
      if (imgNode.dataset.fallbackBound === '1') return;
      imgNode.dataset.fallbackBound = '1';

      imgNode.addEventListener('error', () => replaceBrokenImage(imgNode));

      if (imgNode.complete && imgNode.naturalWidth === 0) {
        replaceBrokenImage(imgNode);
      }
    });
  }

  function init(root) {
    applyCategoryIconTargets(root || document);
    attachImageFallbacks(root || document);
  }

  window.StockArProductVisual = {
    getCategoryIcon,
    buildFallbackHtml,
    applyCategoryIconTargets,
    attachImageFallbacks,
    replaceBrokenImage,
    init,
  };
})();
