// Alpine components for the global app chrome (navbar / future sidebar).
// Kept out of base.html so the shell markup stays readable and these concerns
// carry their own state — moving them into a sidebar is then pure markup/CSS,
// with no getElementById lookups to rewire.

import * as ProxUtils from "./utils.js";

function jobStatusInfo(status) {
  switch (status) {
    case "queued":    return { color: "secondary", icon: "bi-clock", label: "Queued" };
    case "running":   return { color: "primary", icon: "bi-hourglass-split", label: "Running" };
    case "completed": return { color: "success", icon: "bi-check-circle", label: "Done" };
    case "failed":    return { color: "danger", icon: "bi-x-circle", label: "Failed" };
    default:          return { color: "secondary", icon: "", label: status };
  }
}

// Background jobs indicator + dropdown + details modal.
export function globalJobs() {
  return {
    jobs: [],
    selectedJob: null,
    detailsOpen: false,
    _poller: null,

    statusInfo: jobStatusInfo,

    get activeJobs() {
      return this.jobs.filter(j => j.status === "queued" || j.status === "running");
    },

    // Active jobs plus anything completed/failed within the last hour.
    get recentJobs() {
      const hourAgo = Date.now() - 3600000;
      return this.jobs.filter(j => {
        if (j.status === "queued" || j.status === "running") return true;
        const done = j.completed_at ? new Date(j.completed_at).getTime() : null;
        return done !== null && done > hourAgo;
      });
    },

    errorSnippet(job) {
      return job.error ? (String(job.error).substring(0, 50) + "...") : "";
    },

    async load() {
      try {
        const data = await ProxUtils.apiJson("/api/jobs");
        this.jobs = Array.isArray(data) ? data : [];
      } catch (e) {
        // Keep the last known list on transient failure; log for diagnostics.
        console.error("Error loading jobs:", e);
      }
    },

    refresh() { this.load(); },

    async showDetails(jobId) {
      try {
        const job = await ProxUtils.apiJson(`/api/jobs/${jobId}`);
        if (job.error && !job.steps) { ProxUtils.notify("Job not found", "error"); return; }
        this.selectedJob = job;
        this.detailsOpen = true;
      } catch (e) {
        ProxUtils.notify("Error loading job details: " + e.message, "error");
      }
    },

    closeDetails() { this.detailsOpen = false; this.selectedJob = null; },

    async deleteJob(jobId) {
      if (!confirm("Delete this job from the list?")) return;
      try {
        const r = await ProxUtils.apiJson(`/api/jobs/${jobId}`, { method: "DELETE" });
        if (r.success) { this.closeDetails(); this.load(); }
        else ProxUtils.notify(r.error || "Failed to delete job", "error");
      } catch (e) {
        ProxUtils.notify("Error deleting job: " + e.message, "error");
      }
    },

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
      if (saved && saved !== this.currentId) {
        if (this.knownIds.includes(saved)) {
          this.switchTo(saved);
        } else {
          // The remembered cluster is gone — clear it and let the user re-pick.
          try {
            localStorage.removeItem("selectedClusterId");
            localStorage.removeItem("selectedClusterName");
          } catch (_) {}
          if (this.knownIds.length > 0) this.staleOpen = true;
        }
      }
    },

    async switchTo(clusterId, clusterName = null) {
      this.switching = true;
      this.switchingText = "Switching to cluster...";
      this.currentName = clusterName || "Switching…";
      try {
        const data = await ProxUtils.apiJson(`/api/switch-cluster/${clusterId}`, { method: "POST" });
        if (data.success) {
          try {
            localStorage.setItem("selectedClusterId", clusterId);
            localStorage.setItem("selectedClusterName", data.cluster.name);
          } catch (_) {}
          ProxUtils.notify(`Switched to ${data.cluster.name}`, "success");
          setTimeout(() => window.location.reload(), 800);
        } else {
          this.switching = false;
          ProxUtils.notify(data.error || "Failed to switch cluster", "error");
        }
      } catch (e) {
        this.switching = false;
        ProxUtils.notify("Network error while switching cluster", "error");
      }
    },
  };
}
