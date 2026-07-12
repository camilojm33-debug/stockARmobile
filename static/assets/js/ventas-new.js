// Compatibilidad legacy: la logica activa del carrito vive en cart-manager.js.
document.addEventListener('DOMContentLoaded', function() {
  if (typeof loadCart === 'function') {
    loadCart();
  }
});
