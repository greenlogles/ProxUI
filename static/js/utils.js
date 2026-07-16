// static/js/utils.js — shared frontend helpers (ES module)

export function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}

export function formatBytes(bytes, decimals = 1) {
  if (!bytes || bytes <= 0) return '0 B';
  const k = 1024;
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(decimals)} ${units[i]}`;
}

export function formatUptime(seconds) {
  seconds = Number(seconds) || 0;
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  if (days > 0) return `${days}d ${hours}h`;
  const mins = Math.floor((seconds % 3600) / 60);
  return `${hours}h ${mins}m`;
}

export const TASK_TYPE_NAMES = {
  qmstart: 'Start VM', qmstop: 'Stop VM', qmshutdown: 'Shutdown VM',
  qmreset: 'Reset VM', qmreboot: 'Reboot VM', qmigrate: 'Migrate VM',
  qmclone: 'Clone VM', qmcreate: 'Create VM', qmdestroy: 'Destroy VM',
  qmtemplate: 'Create VM template',
  vzstart: 'Start Container', vzstop: 'Stop Container', vzshutdown: 'Shutdown Container',
  vzmigrate: 'Migrate Container', vzclone: 'Clone Container', vzcreate: 'Create Container',
  vzdestroy: 'Destroy Container', vzdump: 'Backup Container',
  download: 'Download ISO', aptupdate: 'Update package database',
  startall: 'Bulk start VMs and Containers', stopall: 'Bulk stop VMs and Containers',
  srvreload: 'SRV networking - reload', vncproxy: 'VNC Console',
  clusterjoin: 'Add node to cluster', resize: 'Disk resize',
};

export function taskTypeName(type) {
  return TASK_TYPE_NAMES[type] || (type ? String(type).toUpperCase() : 'UNKNOWN');
}

const DEMO_MODE = window.DEMO_MODE === true;

export async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let errorMessage;
    try {
      const data = await response.clone().json();
      errorMessage = data.error || data.message || response.statusText;
    } catch {
      errorMessage = response.statusText;
    }
    if (DEMO_MODE && (response.status === 403 || response.status === 401)) {
      errorMessage = 'Demo mode: This action requires write permissions that are not available in the demo.';
    }
    const error = new Error(errorMessage);
    error.status = response.status;
    error.response = response;
    throw error;
  }
  return response;
}

export async function apiJson(url, options = {}) {
  return (await apiFetch(url, options)).json();
}

/**
 * Adaptive poller: calls fn() on a base interval when the tab is active,
 * backs off exponentially when hidden, stops after the 10-minute step,
 * and immediately refreshes + resets when the tab becomes visible again.
 *
 * Backoff when hidden: baseMs → 60s → 2m → 5m → 10m → stop
 */
export function createAdaptivePoller(fn, baseMs = 30000) {
  const hiddenDelays = [60_000, 120_000, 300_000, 600_000];
  let timerId = null;
  let hiddenCount = 0;
  let active = true;

  function scheduleNext() {
    if (!active) return;
    const delay = (document.hidden && hiddenCount > 0)
      ? hiddenDelays[Math.min(hiddenCount - 1, hiddenDelays.length - 1)]
      : baseMs;
    timerId = setTimeout(tick, delay);
  }

  function tick() {
    timerId = null;
    if (!active) return;
    if (document.hidden) {
      hiddenCount++;
      if (hiddenCount > hiddenDelays.length) return; // stop; resumes on focus
    } else {
      hiddenCount = 0;
      fn();
    }
    scheduleNext();
  }

  function onVisible() {
    if (!active || document.hidden) return;
    clearTimeout(timerId);
    timerId = null;
    hiddenCount = 0;
    fn();
    scheduleNext();
  }

  document.addEventListener('visibilitychange', onVisible);
  scheduleNext();

  return {
    destroy() {
      active = false;
      clearTimeout(timerId);
      document.removeEventListener('visibilitychange', onVisible);
    },
  };
}

/**
 * Format a Unix timestamp (seconds) for a chart x-axis label, using the
 * *browser's* local timezone — Date's locale methods are always
 * timezone-correct for whoever is viewing the page, unlike formatting the
 * timestamp on the server (which depends on the server/container's own
 * timezone and is frequently wrong, e.g. Docker images defaulting to UTC).
 */
export function formatChartLabel(unixSeconds, timeframe) {
  const d = new Date(unixSeconds * 1000);
  switch (timeframe) {
    case 'hour':
    case 'day':
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    case 'week':
      return d.toLocaleDateString([], { month: '2-digit', day: '2-digit' }) + ' ' +
        d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    case 'month':
      return d.toLocaleDateString([], { month: '2-digit', day: '2-digit' });
    default: // year
      return d.toLocaleDateString([], { year: 'numeric', month: '2-digit' });
  }
}

/** Format a Unix timestamp (seconds) as a full local date+time string. */
export function formatDateTime(unixSeconds) {
  if (!unixSeconds) return 'Unknown';
  return new Date(unixSeconds * 1000).toLocaleString([], {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

export function notify(message, type = 'info') {
  const cls = type === 'error' ? 'danger' : type;
  const el = document.createElement('div');
  el.className = `alert alert-${cls} alert-dismissible fade show position-fixed`;
  el.style.cssText = 'top:20px;right:20px;z-index:10000;min-width:300px;';
  el.textContent = message;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'btn-close';
  btn.setAttribute('data-bs-dismiss', 'alert');
  el.appendChild(btn);
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}
