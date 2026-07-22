(function () {
  const state = {
    requestId: `offline_${Date.now()}_${Math.random().toString(36).slice(2)}`,
    serviceWorkerReady: false,
  };

  const elements = {
    badge: document.getElementById('offlineConnectivityBadge'),
    badgeCompact: document.getElementById('offlineConnectivityBadgeCompact'),
    pendingCount: document.getElementById('offlinePendingCount'),
    lastSync: document.getElementById('offlineLastSync'),
    syncError: document.getElementById('offlineSyncError'),
    syncNow: document.getElementById('offlineSyncNow'),
  };

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
    if (elements.lastSync) {
      elements.lastSync.textContent = formatLastSync(status.lastSyncAt);
    }
    updateSyncError(status.lastError || '');
    setBadgeState(Boolean(status.online), pendingCount);
    if (elements.syncNow) {
      elements.syncNow.disabled = !state.serviceWorkerReady;
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
