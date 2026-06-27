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
