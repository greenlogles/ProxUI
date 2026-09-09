import * as ProxUtils from "./utils.js";

// Maintenance page: per-node pending apt updates, non-interactive apply-updates
// jobs (SSH), and a reboot action surfaced once a job reports one is required.
export function maintenanceApp(initialNodes, clusterId) {
  return {
    clusterId,
    nodes: (initialNodes || []).map(n => ({
      ...n,
      refreshing: false,
      applying: false,
      rebooting: false,
      jobId: null,
      job: null,
      logOpen: false,
      // Last-known status from the server (set by a prior upgrade job or an
      // explicit reboot-status check) — survives a full page reload.
      rebootRequired: !!n.reboot_required,
      rebootPackages: n.reboot_packages || [],
    })),

    get totalPending() {
      return this.nodes.reduce((a, n) => a + (n.updates ? n.updates.length : 0), 0);
    },
    get nodesPending() {
      return this.nodes.filter(n => n.updates && n.updates.length > 0).length;
    },
    get anyRefreshing() {
      return this.nodes.some(n => n.refreshing);
    },

    async refresh(n) {
      n.refreshing = true;
      try {
        const r = await ProxUtils.apiJson(`/api/node/${n.node}/apt/updates?refresh=1`);
        n.updates = r.updates || [];
        n.online = true;
        n.error = null;
      } catch (e) {
        n.error = e.message;
        n.online = false;
      } finally {
        n.refreshing = false;
      }
      // Re-check reboot-required over SSH too, so it doesn't just reflect
      // whatever the last "Apply updates" job happened to see.
      try {
        const rb = await ProxUtils.apiJson(`/api/node/${n.node}/reboot-status?refresh=1`);
        if (!rb.error) {
          n.rebootRequired = !!rb.reboot_required;
          n.rebootPackages = rb.reboot_packages || [];
        }
      } catch (e) {
        // Leave the last-known reboot status in place on a transient failure.
      }
    },

    async refreshAll() {
      for (const n of this.nodes) {
        await this.refresh(n);
      }
    },

    async applyUpdates(n) {
      if (!confirm(`Apply ${n.updates.length} pending update(s) on ${n.node}?\n\nThis runs "apt-get update && apt-get dist-upgrade" non-interactively over SSH, then removes unused packages and cleans the apt cache.`)) return;
      n.applying = true;
      n.logOpen = true;
      n.job = null;
      try {
        const r = await ProxUtils.apiJson(`/api/node/${n.node}/apt/upgrade`, { method: "POST" });
        n.jobId = r.job_id;
        this._pollJob(n);
      } catch (e) {
        n.applying = false;
        ProxUtils.notify("Failed to start upgrade: " + e.message, "error");
      }
    },

    _pollJob(n) {
      const tick = async () => {
        if (!n.jobId) return;
        try {
          const job = await ProxUtils.apiJson(`/api/jobs/${n.jobId}`);
          n.job = job;
          if (job.status === "completed" || job.status === "failed") {
            n.applying = false;
            if (job.status === "completed" && job.result) {
              n.rebootRequired = !!job.result.reboot_required;
              n.rebootPackages = job.result.reboot_packages || [];
              await this.refresh(n);
            } else if (job.status === "failed") {
              ProxUtils.notify(`Update failed on ${n.node}: ${job.error || "unknown error"}`, "error");
            }
            return;
          }
        } catch (e) {
          // Keep last known job state on a transient poll failure.
        }
        setTimeout(tick, 2000);
      };
      tick();
    },

    async reboot(n) {
      if (!confirm(`Reboot node ${n.node} now?\n\nThis restarts the physical/virtual host immediately. Running VMs and containers on this node will be interrupted unless HA-managed or migrated first.`)) return;
      n.rebooting = true;
      try {
        const r = await ProxUtils.apiJson(`/api/node/${n.node}/reboot`, { method: "POST" });
        ProxUtils.notify(r.message || `Reboot sent to ${n.node}`, "success");
        n.rebootRequired = false;
      } catch (e) {
        ProxUtils.notify("Reboot failed: " + e.message, "error");
      } finally {
        n.rebooting = false;
      }
    },

    // Reattach to any upgrade job still running server-side for a node (e.g.
    // the page was reloaded mid-upgrade) instead of showing a blank/idle card
    // for a job that's actually still going.
    async init() {
      let jobs = [];
      try {
        jobs = await ProxUtils.apiJson("/api/jobs");
      } catch (e) {
        return;
      }
      for (const job of jobs || []) {
        if (job.type !== "node_apt_upgrade") continue;
        if (!job.params || job.params.cluster_id !== this.clusterId) continue;
        if (job.status !== "queued" && job.status !== "running") continue;
        const n = this.nodes.find(x => x.node === (job.params && job.params.node));
        if (!n) continue;
        n.jobId = job.id;
        n.job = job;
        n.applying = true;
        n.logOpen = true;
        this._pollJob(n);
      }
    },
  };
}
