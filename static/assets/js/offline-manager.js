(function () {
  const state = {
    requestId: `offline_${Date.now()}_${Math.random().toString(36).slice(2)}`,
    serviceWorkerReady: false,
    inflightForms: new WeakSet(),
  };

  const elements = {
    badge: document.getElementById('offlineConnectivityBadge'),
    badgeCompact: document.getElementById('offlineConnectivityBadgeCompact'),
    pendingCount: document.getElementById('offlinePendingCount'),
    syncedCount: document.getElementById('offlineSyncedCount'),
    errorCount: document.getElementById('offlineErrorCount'),
    lastSync: document.getElementById('offlineLastSync'),
    syncError: document.getElementById('offlineSyncError'),
    progressBar: document.getElementById('offlineSyncProgressBar'),
    progressLabel: document.getElementById('offlineSyncProgressLabel'),
    currentOperation: document.getElementById('offlineCurrentOperation'),
    syncNow: document.getElementById('offlineSyncNow'),
  };

  const CRITICAL_PATH_RULES = [
    /^\/productos\/(add|edit\/|delete\/|import)/,
    /^\/clientes\/(add|post|edit\/|delete\/|api\/quick-create)/,
    /^\/compras\/(?:$|proveedores(?:\/\d+)?(?:\/(?:update|toggle))?)/,
    /^\/gastos\//,
    /^\/caja\//,
    /^\/ventas\/(?:checkout|edit\/|delete\/|view\/\d+|api\/checkout|api\/mp-qr\/create|api\/mp-qr\/finalize)/,
  ];

  function isCriticalOfflineForm(form) {
    const action = new URL(form.action || window.location.href, window.location.origin);
    const pathname = action.pathname;
    if (form.dataset.offlineQueue === 'false') return false;
    return CRITICAL_PATH_RULES.some((rule) => rule.test(pathname));
  }

  function ensureOfflineUuid(form) {
    let input = form.querySelector('input[name="offline_uuid"]');
    if (!input) {
      input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'offline_uuid';
      form.appendChild(input);
    }
    if (!input.value) {
      input.value = typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `offline_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    }
    return input.value;
  }

  function setProgress(status) {
    const total = Number(status?.totalCount || 0);
    const processed = Number(status?.syncedCount || 0) + Number(status?.errorCount || 0);
    const progress = Number.isFinite(status?.progress) ? Number(status.progress) : (total > 0 ? Math.round((processed / total) * 100) : 100);
    if (elements.syncedCount) elements.syncedCount.textContent = String(Number(status?.syncedCount || 0));
    if (elements.errorCount) elements.errorCount.textContent = String(Number(status?.errorCount || 0));
    if (elements.progressBar) elements.progressBar.style.width = `${Math.max(0, Math.min(100, progress))}%`;
    if (elements.progressLabel) {
      elements.progressLabel.textContent = status?.syncing
        ? `Sincronizando ${processed}/${total || processed} operaciones`
        : (total > 0 ? `Última sincronización: ${processed}/${total}` : 'Sincronización al día');
    }
    if (elements.currentOperation) {
      elements.currentOperation.textContent = status?.syncing && status.currentOperation
        ? `Operación actual: ${status.currentOperation}`
        : '';
    }
  }

  function notify(message, type = 'success') {
    if (typeof window.showNotification === 'function') {
      window.showNotification(message, type);
      return;
    }
    const toast = document.createElement('div');
    toast.className = `alert alert-${type === 'danger' ? 'danger' : type === 'warning' ? 'warning' : 'info'} position-fixed top-0 end-0 m-3 shadow`;
    toast.style.zIndex = '1090';
    toast.textContent = message;
    document.body.appendChild(toast);
    window.setTimeout(() => toast.remove(), 4200);
  }

  function formatLastSync(value) {
    if (!value) return 'Nunca';
    try {
      return new Intl.DateTimeFormat('es-AR', {
        dateStyle: 'short',
        timeStyle: 'short',
      }).format(new Date(value));
    } catch (error) {
      return value;
    }
  }

  function setBadgeState(online, pendingCount) {
    const label = online ? (pendingCount > 0 ? 'Online con cola' : 'Online') : 'Offline';
    const classes = online
      ? (pendingCount > 0 ? 'badge text-bg-warning' : 'badge text-bg-success')
      : 'badge text-bg-danger';

    [elements.badge, elements.badgeCompact].forEach((node) => {
      if (!node) return;
      node.textContent = label;
      node.className = classes;
    });
  }

  function updateSyncError(message) {
    if (!elements.syncError) return;
    if (message) {
      elements.syncError.textContent = message;
      elements.syncError.classList.remove('d-none');
    } else {
      elements.syncError.textContent = '';
      elements.syncError.classList.add('d-none');
    }
  }

  function renderStatus(status) {
    if (!status) {
      setBadgeState(navigator.onLine, 0);
      return;
    }

    const pendingCount = Number(status.pendingCount || 0);
    if (elements.pendingCount) {
      elements.pendingCount.textContent = String(pendingCount);
    }
    setProgress(status);
    if (elements.lastSync) {
      elements.lastSync.textContent = formatLastSync(status.lastSyncAt);
    }
    updateSyncError(status.lastError || '');
    setBadgeState(Boolean(status.online), pendingCount);
    if (elements.syncNow) {
      elements.syncNow.disabled = !state.serviceWorkerReady || Boolean(status.syncing);
    }
  }

  function postMessageToWorker(message) {
    if (!('serviceWorker' in navigator)) return Promise.resolve();
    return navigator.serviceWorker.ready.then((registration) => {
      state.serviceWorkerReady = true;
      registration.active?.postMessage({ ...message, requestId: state.requestId });
      if (elements.syncNow) {
        elements.syncNow.disabled = false;
      }
    }).catch(() => {});
  }

  function requestStatus() {
    return postMessageToWorker({ type: 'GET_OFFLINE_STATUS' });
  }

  async function submitFormOffline(form) {
    if (state.inflightForms.has(form)) return;
    state.inflightForms.add(form);
    const submitter = form.__offlineSubmitter || null;
    const submitButtons = Array.from(form.querySelectorAll('button[type="submit"], input[type="submit"]'));
    submitButtons.forEach((button) => { button.disabled = true; });

    try {
      const uuid = ensureOfflineUuid(form);
      const formData = new FormData(form);
      const response = await fetch(form.action, {
        method: (form.method || 'POST').toUpperCase(),
        body: formData,
        credentials: 'same-origin',
        headers: {
          'X-Offline-Request-Id': uuid,
          'X-Requested-With': 'XMLHttpRequest',
        },
      });
      const contentType = (response.headers.get('content-type') || '').toLowerCase();
      let payload = null;
      if (contentType.includes('application/json')) {
        payload = await response.json();
      }
      if (payload?.queued) {
        notify('Operación guardada en cola offline. Se sincronizará al volver Internet.', 'warning');
      } else if (response.ok && payload?.redirect_url) {
        window.location.href = payload.redirect_url;
        return;
      } else if (response.ok && response.redirected) {
        window.location.href = response.url;
        return;
      } else if (response.ok) {
        window.location.reload();
        return;
      } else {
        throw new Error(payload?.error || `No se pudo enviar el formulario (${response.status}).`);
      }
    } catch (error) {
      notify(error.message || 'No se pudo enviar el formulario.', 'danger');
    } finally {
      submitButtons.forEach((button) => { button.disabled = false; });
      state.inflightForms.delete(form);
      delete form.__offlineSubmitter;
    }
  }

  function installFormInterceptors() {
    document.addEventListener('submit', function (event) {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      if ((form.method || 'GET').toLowerCase() !== 'post') return;
      if (!navigator.onLine && isCriticalOfflineForm(form)) {
        event.preventDefault();
        submitFormOffline(form);
      }
    }, true);

    document.addEventListener('click', function (event) {
      const button = event.target.closest('button[type="submit"], input[type="submit"]');
      if (!button) return;
      const form = button.form;
      if (!form || (form.method || 'GET').toLowerCase() !== 'post') return;
      form.__offlineSubmitter = button;
      if (!navigator.onLine && isCriticalOfflineForm(form)) {
        button.dataset.offlinePending = 'true';
      }
    }, true);
  }

  async function flushQueue() {
    await postMessageToWorker({ type: 'FLUSH_QUEUE' });
    requestStatus();
  }

  if (elements.syncNow) {
    elements.syncNow.addEventListener('click', function () {
      flushQueue();
    });
  }

  window.addEventListener('online', function () {
    renderStatus({ online: true, pendingCount: Number(elements.pendingCount?.textContent || 0), lastSyncAt: null, lastError: '' });
    flushQueue();
  });

  window.addEventListener('offline', function () {
    renderStatus({ online: false, pendingCount: Number(elements.pendingCount?.textContent || 0), lastSyncAt: null, lastError: 'Sin conexion' });
  });

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', function (event) {
      const data = event.data || {};
      if (data.type === 'OFFLINE_QUEUE_STATUS') {
        renderStatus(data.status);
      }
    });
    navigator.serviceWorker.ready.then(function () {
      state.serviceWorkerReady = true;
      if (elements.syncNow) {
        elements.syncNow.disabled = false;
      }
      requestStatus();
    }).catch(function () {});
  }

  installFormInterceptors();

  if (elements.syncNow) {
    elements.syncNow.disabled = true;
  }
  renderStatus({
    online: navigator.onLine,
    pendingCount: Number(elements.pendingCount?.textContent || 0),
    lastSyncAt: null,
    lastError: '',
  });

  window.stockarmobileOffline = {
    refresh: requestStatus,
    flush: flushQueue,
  };
})();
