(function () {
  'use strict';

  const FALLBACK_SCRIPT_URL = 'https://unpkg.com/html5-qrcode';
  const DUPLICATE_WINDOW_MS = 1200;
  const NATIVE_FORMATS = ['ean_13', 'ean_8', 'upc_a', 'upc_e', 'code_128', 'code_39', 'itf'];

  let activeSession = null;
  let fallbackLoader = null;

  function createError(message, code) {
    const error = new Error(message);
    error.code = code;
    return error;
  }

  function loadFallbackLibrary() {
    if (window.Html5Qrcode) return Promise.resolve();
    if (fallbackLoader) return fallbackLoader;

    fallbackLoader = new Promise(function (resolve, reject) {
      const script = document.createElement('script');
      script.src = FALLBACK_SCRIPT_URL;
      script.async = true;
      script.onload = resolve;
      script.onerror = function () {
        reject(createError('No se pudo cargar el lector de codigos.', 'fallback_unavailable'));
      };
      document.head.appendChild(script);
    });
    return fallbackLoader;
  }

  function mapCameraError(error) {
    const name = error && error.name;
    if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
      return createError('Permiso de camara bloqueado.', 'permission_denied');
    }
    if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
      return createError('No se encontro una camara disponible.', 'camera_not_found');
    }
    if (name === 'NotReadableError' || name === 'TrackStartError') {
      return createError('La camara esta siendo usada por otra aplicacion.', 'camera_busy');
    }
    if (name === 'SecurityError') {
      return createError('La camara requiere HTTPS o localhost.', 'insecure_context');
    }
    return createError('No se pudo iniciar la camara.', 'camera_error');
  }

  function createView(session) {
    const root = document.createElement('div');
    root.className = 'barcode-scanner-overlay';
    root.innerHTML = [
      '<div class="barcode-scanner-dialog" role="dialog" aria-modal="true" aria-label="Escanear codigo de barras">',
      '  <div class="barcode-scanner-header">',
      '    <strong>Escanear codigo</strong>',
      '    <button type="button" class="barcode-scanner-close" aria-label="Cancelar">Cerrar</button>',
      '  </div>',
      '  <div class="barcode-scanner-preview">',
      '    <video class="barcode-scanner-video" playsinline muted></video>',
      '    <div class="barcode-scanner-guide" aria-hidden="true"></div>',
      '  </div>',
      '  <div class="barcode-scanner-status" role="status">Preparando camara...</div>',
      '</div>'
    ].join('');

    root.querySelector('.barcode-scanner-close').addEventListener('click', function () {
      closeSession(session, true);
    });
    root.addEventListener('click', function (event) {
      if (event.target === root) closeSession(session, true);
    });
    document.addEventListener('keydown', session.escapeHandler);
    document.body.appendChild(root);

    session.root = root;
    session.video = root.querySelector('.barcode-scanner-video');
    session.status = root.querySelector('.barcode-scanner-status');
  }

  function setStatus(session, message) {
    if (session.status) session.status.textContent = message;
  }

  function shouldIgnoreCode(session, barcode) {
    const now = Date.now();
    if (session.lastCode === barcode && now - session.lastCodeAt < DUPLICATE_WINDOW_MS) return true;
    session.lastCode = barcode;
    session.lastCodeAt = now;
    return false;
  }

  function detected(session, rawBarcode) {
    const barcode = String(rawBarcode == null ? '' : rawBarcode).trim();
    if (!barcode || session.closed || shouldIgnoreCode(session, barcode)) return;

    session.detecting = true;
    closeSession(session, false);
    try {
      session.options.onDetected(barcode);
    } catch (error) {
      setTimeout(function () { throw error; }, 0);
    }
  }

  async function startNativeDetector(session) {
    const supported = typeof BarcodeDetector.getSupportedFormats === 'function'
      ? await BarcodeDetector.getSupportedFormats()
      : NATIVE_FORMATS;
    const formats = NATIVE_FORMATS.filter(function (format) { return supported.includes(format); });
    if (!formats.length) throw createError('El navegador no soporta formatos de codigo compatibles.', 'format_not_supported');

    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' } },
      audio: false
    });
    session.stream = stream;
    session.video.srcObject = stream;
    await session.video.play();
    session.detector = new BarcodeDetector({ formats: formats });
    setStatus(session, 'Apunta al codigo de barras dentro de la guia.');

    const scanFrame = async function () {
      if (session.closed || session.detecting) return;
      try {
        const codes = await session.detector.detect(session.video);
        if (codes.length) detected(session, codes[0].rawValue);
      } catch (_) {
        // A transient frame error should not close the scanner.
      }
      if (!session.closed && !session.detecting) requestAnimationFrame(scanFrame);
    };
    requestAnimationFrame(scanFrame);
  }

  async function startFallbackDetector(session) {
    await loadFallbackLibrary();
    const readerId = 'barcode-scanner-reader-' + Date.now();
    session.video.classList.add('barcode-scanner-hidden');
    const reader = document.createElement('div');
    reader.id = readerId;
    reader.className = 'barcode-scanner-fallback';
    session.root.querySelector('.barcode-scanner-preview').appendChild(reader);

    session.fallback = new Html5Qrcode(readerId);
    setStatus(session, 'Abriendo camara para leer el codigo...');
    await session.fallback.start(
      { facingMode: 'environment' },
      { fps: 12, qrbox: { width: 260, height: 150 }, aspectRatio: 1.5 },
      function (decodedText) { detected(session, decodedText); },
      function () {}
    );
    setStatus(session, 'Apunta al codigo de barras dentro de la guia.');
  }

  async function closeSession(session, cancelled) {
    if (!session || session.closed) return;
    session.closed = true;
    if (activeSession === session) activeSession = null;

    document.removeEventListener('keydown', session.escapeHandler);
    if (session.stream) {
      session.stream.getTracks().forEach(function (track) { track.stop(); });
      session.stream = null;
    }
    if (session.fallback) {
      try {
        await session.fallback.stop();
      } catch (_) {
        // The fallback reader may already be stopped after a successful read.
      }
      try {
        await session.fallback.clear();
      } catch (_) {}
      session.fallback = null;
    }
    if (session.root) session.root.remove();
    if (cancelled) session.options.onCancel();
  }

  async function openScanner(options) {
    const normalizedOptions = Object.assign({ onDetected: function () {}, onError: function () {}, onCancel: function () {} }, options || {});
    if (typeof normalizedOptions.onDetected !== 'function') throw createError('onDetected debe ser una funcion.', 'invalid_callback');
    if (!window.isSecureContext) {
      const error = createError('La camara requiere HTTPS o localhost.', 'insecure_context');
      normalizedOptions.onError(error);
      return null;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      const error = createError('Este navegador no permite usar la camara.', 'camera_not_supported');
      normalizedOptions.onError(error);
      return null;
    }

    if (activeSession) await closeSession(activeSession, true);
    const session = {
      options: normalizedOptions,
      root: null,
      video: null,
      status: null,
      stream: null,
      fallback: null,
      detector: null,
      closed: false,
      detecting: false,
      lastCode: '',
      lastCodeAt: 0,
      escapeHandler: function (event) {
        if (event.key === 'Escape') closeSession(session, true);
      }
    };
    activeSession = session;
    createView(session);

    try {
      if ('BarcodeDetector' in window) {
        await startNativeDetector(session);
      } else {
        await startFallbackDetector(session);
      }
      return { close: function () { return closeSession(session, true); } };
    } catch (error) {
      await closeSession(session, false);
      normalizedOptions.onError(mapCameraError(error));
      return null;
    }
  }

  function installStyles() {
    if (document.getElementById('barcode-scanner-styles')) return;
    const style = document.createElement('style');
    style.id = 'barcode-scanner-styles';
    style.textContent = [
      '.barcode-scanner-overlay{position:fixed;inset:0;z-index:2000;display:grid;place-items:center;padding:1rem;background:rgba(0,0,0,.62)}',
      '.barcode-scanner-dialog{width:min(100%,34rem);overflow:hidden;background:#fff;border-radius:8px;box-shadow:0 1rem 3rem rgba(0,0,0,.35)}',
      '.barcode-scanner-header{display:flex;align-items:center;justify-content:space-between;padding:1rem;border-bottom:1px solid #dee2e6}',
      '.barcode-scanner-close{border:0;background:transparent;color:#495057;font:inherit;cursor:pointer}',
      '.barcode-scanner-preview{position:relative;min-height:18rem;background:#111;overflow:hidden}',
      '.barcode-scanner-video,.barcode-scanner-fallback{width:100%;min-height:18rem;object-fit:cover}',
      '.barcode-scanner-hidden{display:none}',
      '.barcode-scanner-guide{position:absolute;left:12%;right:12%;top:34%;height:30%;border:3px solid #20c997;border-radius:6px;box-shadow:0 0 0 999px rgba(0,0,0,.13);pointer-events:none}',
      '.barcode-scanner-status{padding:.8rem 1rem;color:#495057;font-size:.9rem}'
    ].join('');
    document.head.appendChild(style);
  }

  installStyles();
  window.StockArBarcodeScanner = { openScanner: openScanner };
  window.openScanner = openScanner;
}());
