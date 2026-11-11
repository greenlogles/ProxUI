// Alpine components for the global app chrome (navbar / future sidebar).
// Kept out of base.html so the shell markup stays readable and these concerns
// carry their own state — moving them into a sidebar is then pure markup/CSS,
// with no getElementById lookups to rewire.

import * as ProxUtils from "./utils.js";

// Long-lived console sessions are tasks too, but they'd keep the badge lit
// for as long as a console is open, so the navbar leaves them out.
const QUIET_TASK_TYPES = new Set(["vncproxy", "vncshell", "termproxy", "spiceproxy"]);

// Background activity indicator + dropdown: ProxUI jobs and native Proxmox
// tasks from /api/activity. Each entry links to its detail page.
export function globalJobs() {
  return {
    jobs: [],
    _poller: null,

    statusInfo: ProxUtils.activityStatus,
    title: ProxUtils.activityTitle,
    detailUrl: ProxUtils.activityUrl,

    get visible() {
      return this.jobs.filter(j => !QUIET_TASK_TYPES.has(j.type));
    },

    get activeJobs() {
      return this.visible.filter(ProxUtils.activityIsActive);
    },

    // Active entries plus anything that finished within the last hour.
    get recentJobs() {
      const hourAgo = Date.now() / 1000 - 3600;
      return this.visible
        .filter(j => ProxUtils.activityIsActive(j) || (j.ended_at && j.ended_at > hourAgo))
        .slice(0, 20);
    },

    async load() {
      try {
        const data = await ProxUtils.apiJson("/api/activity?limit=50");
        this.jobs = Array.isArray(data.items) ? data.items : [];
      } catch (e) {
        // Keep the last known list on transient failure; log for diagnostics.
        console.error("Error loading activity:", e);
      }
    },

    refresh() { this.load(); },

    init() {
      this.load();
      this._poller = ProxUtils.createAdaptivePoller(() => this.load(), 3000);
    },

    destroy() { this._poller?.destroy(); },
  };
}

// Cluster selector + auto-switch to the last-used cluster + stale-cluster prompt.
export function clusterSwitcher(currentId, currentName, knownIds) {
  return {
    currentId,
    currentName,
    knownIds: knownIds || [],
    switching: false,
    switchingText: "",
    staleOpen: false,

    init() {
      let saved = null;
      try { saved = localStorage.getItem("selectedClusterId"); } catch (_) {}
      if (!saved || saved === this.currentId) return;   // already on the saved cluster

      if (!this.knownIds.includes(saved)) {
        // The remembered cluster is gone — clear it and let the user re-pick.
        try {
          localStorage.removeItem("selectedClusterId");
          localStorage.removeItem("selectedClusterName");
        } catch (_) {}
        if (this.knownIds.length > 0) this.staleOpen = true;
        return;
      }

      // One-shot guard: auto-restore the saved cluster at most once per tab
      // session. If the switch doesn't "stick" server-side (shared global reset,
      // flaky node), this prevents an endless switch→reload loop that would
      // leave the blocking overlay stuck on screen. Manual switches are exempt.
      let attempted = null;
      try { attempted = sessionStorage.getItem("clusterAutoSwitched"); } catch (_) {}
      if (attempted === saved) return;
      try { sessionStorage.setItem("clusterAutoSwitched", saved); } catch (_) {}
      this.switchTo(saved, null, true);
    },

    // `auto` marks an on-load auto-restore (vs. an explicit user click).
    // Auto-restore runs quietly (no blocking overlay): the user is already
    // seeing valid data, so a slow/failed reconnect must not freeze the UI —
    // it just reloads on success, or stays on the current cluster on failure.
    async switchTo(clusterId, clusterName = null, auto = false) {
      if (!auto) {
        this.switching = true;
        this.switchingText = "Switching to cluster…";
        this.currentName = clusterName || "Switching…";
      }
      try {
        const data = await ProxUtils.apiJson(`/api/switch-cluster/${clusterId}`, { method: "POST" });
        if (data.success) {
          try {
            localStorage.setItem("selectedClusterId", clusterId);
            localStorage.setItem("selectedClusterName", data.cluster.name);
          } catch (_) {}
          if (!auto) ProxUtils.notify(`Switched to ${data.cluster.name}`, "success");
          setTimeout(() => window.location.reload(), auto ? 200 : 800);
        } else {
          this.switching = false;
          if (!auto) ProxUtils.notify(data.error || "Failed to switch cluster", "error");
        }
      } catch (e) {
        this.switching = false;
        if (!auto) ProxUtils.notify("Network error while switching cluster", "error");
      }
    },

    // Escape hatch so a slow/hung switch can never permanently trap the UI.
    cancelSwitch() {
      this.switching = false;
    },
  };
}
