import * as ProxUtils from "./utils.js";

const MODE_LABELS = { snapshot: "Snapshot", suspend: "Suspend", stop: "Stop" };

function emptyJob() {
  return {
    id: "", enabled: true, schedule: "", storage: "", selection: "vmid",
    vmid: [], pool: "", exclude: [], mode: "snapshot", compress: "zstd",
    "keep-last": "", "notes-template": "",
  };
}

// Backups page: scheduled vzdump jobs, backup storage usage, and stored backups
// grouped per guest (including guests with no backup at all, and backups whose
// guest no longer exists).
export function backupsApp(initial) {
  return {
    jobs: initial.jobs || [],
    storages: initial.storages || [],
    guests: initial.guests || [],
    pools: initial.pools || [],
    totals: initial.totals ||
      { count: 0, size: 0, real: 0, covered: 0, guests: 0, latest: 0 },

    search: "",
    storageFilter: "",
    coverage: "",
    expanded: [],
    loading: false,
    busyJob: null,
    busyGuest: null,
    busyVolid: null,
    dialog: null,
    _modals: {},
    form: { storage: "", mode: "snapshot", compress: "zstd" },

    jobForm: emptyJob(),
    jobError: "",
    jobSaving: false,

    configVolid: "",
    configText: "",
    configError: "",
    configLoading: false,

    fmtBytes: ProxUtils.formatBytes,
    fmtDate: ProxUtils.formatDateTime,

    /** Relative age ("3d ago"), which reads better than a timestamp for recency. */
    fmtAgo(unixSeconds) {
      if (!unixSeconds) return "Never";
      const mins = Math.floor((Date.now() / 1000 - unixSeconds) / 60);
      if (mins < 1) return "just now";
      if (mins < 60) return `${mins}m ago`;
      const hours = Math.floor(mins / 60);
      if (hours < 24) return `${hours}h ago`;
      const days = Math.floor(hours / 24);
      if (days < 31) return `${days}d ago`;
      return ProxUtils.formatDateTime(unixSeconds).split(",")[0];
    },

    usedPct(s) {
      if (!s.total) return 0;
      return Math.min(100, (s.used / s.total) * 100);
    },

    /** How much of the logical backup data the storage does not actually store. */
    savingPct(s) {
      return s.ratio ? Math.round((1 - s.ratio) * 100) : 0;
    },

    sizeText(row) {
      if (row.kind === "backup") return ProxUtils.formatBytes(row.backup.real ?? row.backup.size);
      return row.guest.count ? ProxUtils.formatBytes(row.guest.real ?? row.guest.size) : "—";
    },

    sizeTitle(row) {
      const logical = row.kind === "backup" ? row.backup.size : row.guest.size;
      if (!logical) return "";
      return `Logical size: ${ProxUtils.formatBytes(logical)}`;
    },

    /** Real guests only — a job cannot select an orphan, and templates are skipped. */
    get selectableGuests() {
      return this.guests.filter(g => !g.orphan);
    },

    countFor(storage) {
      return this.guests.reduce(
        (a, g) => a + g.backups.filter(b => b.storage === storage).length, 0);
    },

    selectionLabel(j) {
      if (j.vmid) {
        const ids = String(j.vmid).split(",").filter(Boolean);
        return ids.length > 6
          ? `${ids.slice(0, 6).join(", ")} +${ids.length - 6} more`
          : ids.join(", ");
      }
      if (j.pool) return `Pool: ${j.pool}`;
      if (j.all) return j.exclude ? `All except ${j.exclude}` : "All guests";
      return "—";
    },

    retentionLabel(j) {
      const prune = j["prune-backups"];
      if (prune && typeof prune === "object") {
        const parts = Object.entries(prune)
          .filter(([, v]) => v !== undefined && v !== null && v !== "")
          .map(([k, v]) => `${k.replace("keep-", "")}: ${v}`);
        if (parts.length) return parts.join(", ");
      }
      if (j.maxfiles) return `max ${j.maxfiles}`;
      return "—";
    },

    shortVolid(volid) {
      const idx = volid.indexOf(":");
      return idx === -1 ? volid : volid.slice(idx + 1);
    },

    get visibleGuests() {
      const q = this.search.trim().toLowerCase();
      return this.guests.filter(g => {
        if (q && !`${g.name} ${g.vmid}`.toLowerCase().includes(q)) return false;
        if (this.storageFilter &&
            !g.backups.some(b => b.storage === this.storageFilter)) return false;
        if (this.coverage === "none" && (g.count > 0 || g.orphan || g.template)) return false;
        if (this.coverage === "some" && g.count === 0) return false;
        if (this.coverage === "orphan" && !g.orphan) return false;
        return true;
      });
    },

    /** Guest rows interleaved with the backup rows of whichever are expanded. */
    get visibleRows() {
      const rows = [];
      for (const g of this.visibleGuests) {
        rows.push({ kind: "guest", key: `g-${g.vmid}`, guest: g });
        if (!this.expanded.includes(g.vmid)) continue;
        for (const b of this.visibleBackups(g)) {
          rows.push({ kind: "backup", key: `b-${b.volid}`, guest: g, backup: b });
        }
      }
      return rows;
    },

    visibleBackups(g) {
      return this.storageFilter
        ? g.backups.filter(b => b.storage === this.storageFilter)
        : g.backups;
    },

    rowClass(row) {
      if (row.kind === "backup") return "sub-row";
      if (row.guest.count === 0 && !row.guest.template && !row.guest.orphan) {
        return "no-backup-row";
      }
      return "";
    },

    toggle(g) {
      if (g.count === 0) return;
      const i = this.expanded.indexOf(g.vmid);
      if (i === -1) this.expanded.push(g.vmid);
      else this.expanded.splice(i, 1);
    },

    async reload() {
      this.loading = true;
      try {
        const r = await ProxUtils.apiJson("/api/backups");
        this.jobs = r.jobs || [];
        this.storages = r.storages || [];
        this.guests = r.guests || [];
        this.recomputeTotals();
      } catch (e) {
        ProxUtils.notify("Failed to refresh backups: " + e.message, "error");
      } finally {
        this.loading = false;
      }
    },

    recomputeTotals() {
      const all = this.guests.flatMap(g => g.backups);
      const real = this.guests.filter(g => !g.orphan && !g.template);
      this.totals = {
        count: all.length,
        size: all.reduce((a, b) => a + (b.size || 0), 0),
        real: this.storages.reduce((a, s) => a + (s.backup_used || 0), 0),
        estimated: this.storages.some(s => s.dedup),
        covered: real.filter(g => g.count > 0).length,
        guests: real.length,
        latest: all.reduce((a, b) => Math.max(a, b.ctime || 0), 0),
        orphans: this.guests.filter(g => g.orphan).length,
      };
    },

    async toggleJob(j) {
      this.busyJob = j.id;
      try {
        const r = await ProxUtils.apiJson(`/api/backups/job/${j.id}/toggle`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled: !j.enabled }),
        });
        j.enabled = r.enabled;
        ProxUtils.notify(r.message, "success");
      } catch (e) {
        ProxUtils.notify("Failed to update job: " + e.message, "error");
      } finally {
        this.busyJob = null;
      }
    },

    async runJob(j) {
      const what = this.selectionLabel(j);
      if (!confirm(`Run backup job ${j.id} now?\n\nGuests: ${what}\nStorage: ${j.storage}` +
                   `\nMode: ${MODE_LABELS[j.mode] || j.mode || "Snapshot"}` +
                   `\n\nProgress is shown on the Tasks page.`)) return;
      this.busyJob = j.id;
      try {
        const r = await ProxUtils.apiJson(`/api/backups/job/${j.id}/run`, { method: "POST" });
        ProxUtils.notify(r.message, "success");
        (r.failed || []).forEach(f => ProxUtils.notify(f, "error"));
      } catch (e) {
        ProxUtils.notify("Failed to start job: " + e.message, "error");
      } finally {
        this.busyJob = null;
      }
    },

    backupNow(g) {
      this.form = {
        storage: this.storages.length ? this.storages[0].storage : "",
        mode: "snapshot",
        compress: "zstd",
      };
      this.dialog = g;
      this._modal("backupModal").show();
    },

    _modal(ref) {
      if (!this._modals[ref]) {
        this._modals[ref] = new bootstrap.Modal(this.$refs[ref]);
      }
      return this._modals[ref];
    },

    newJob() {
      this.jobForm = emptyJob();
      this.jobForm.storage = this.storages.length ? this.storages[0].storage : "";
      this.jobError = "";
      this._modal("jobModal").show();
    },

    editJob(j) {
      const prune = j["prune-backups"];
      this.jobForm = {
        id: j.id,
        enabled: !!j.enabled,
        schedule: j.schedule || "",
        storage: j.storage || "",
        selection: j.pool ? "pool" : (j.all ? "all" : "vmid"),
        vmid: String(j.vmid || "").split(",").filter(Boolean),
        pool: j.pool || "",
        exclude: String(j.exclude || "").split(",").filter(Boolean),
        mode: j.mode || "snapshot",
        compress: j.compress || "zstd",
        "keep-last": (prune && prune["keep-last"]) || "",
        "notes-template": j["notes-template"] || "",
      };
      this.jobError = "";
      this._modal("jobModal").show();
    },

    async submitJob() {
      this.jobSaving = true;
      this.jobError = "";
      const editing = !!this.jobForm.id;
      try {
        await ProxUtils.apiJson(
          editing ? `/api/backups/job/${this.jobForm.id}` : "/api/backups/job",
          {
            method: editing ? "PUT" : "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(this.jobForm),
          });
        this._modal("jobModal").hide();
        ProxUtils.notify(editing ? "Backup job updated." : "Backup job created.", "success");
        await this.reload();
      } catch (e) {
        this.jobError = e.message;
      } finally {
        this.jobSaving = false;
      }
    },

    async deleteJob(j) {
      if (!confirm(`Delete backup job ${j.id}?\n\nSchedule: ${j.schedule || "—"}` +
                   `\nGuests: ${this.selectionLabel(j)}` +
                   `\n\nStored backups are kept; only the schedule is removed.`)) return;
      this.busyJob = j.id;
      try {
        await ProxUtils.apiJson(`/api/backups/job/${j.id}`, { method: "DELETE" });
        this.jobs = this.jobs.filter(x => x.id !== j.id);
        ProxUtils.notify("Backup job deleted.", "success");
      } catch (e) {
        ProxUtils.notify("Failed to delete job: " + e.message, "error");
      } finally {
        this.busyJob = null;
      }
    },

    async showConfig(b) {
      this.configVolid = b.volid;
      this.configText = "";
      this.configError = "";
      this.configLoading = true;
      this._modal("configModal").show();
      try {
        const q = new URLSearchParams({ volid: b.volid, node: b.node });
        const r = await ProxUtils.apiJson(`/api/backups/content/config?${q}`);
        this.configText = r.config || "(empty)";
      } catch (e) {
        this.configError = e.message;
      } finally {
        this.configLoading = false;
      }
    },

    async submitBackup() {
      const g = this.dialog;
      if (!g) return;
      this._modal("backupModal").hide();
      this.dialog = null;
      this.busyGuest = g.vmid;
      try {
        const r = await ProxUtils.apiJson(`/api/backups/guest/${g.vmid}/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this.form),
        });
        ProxUtils.notify(r.message + " Progress is shown on the Tasks page.", "success");
      } catch (e) {
        ProxUtils.notify("Backup failed to start: " + e.message, "error");
      } finally {
        this.busyGuest = null;
      }
    },

    async removeBackup(g, b) {
      if (!confirm(`Delete this backup?\n\n${b.volid}\n${ProxUtils.formatBytes(b.size)}` +
                   ` from ${ProxUtils.formatDateTime(b.ctime)}\n\nThis cannot be undone.`)) return;
      this.busyVolid = b.volid;
      try {
        await ProxUtils.apiJson("/api/backups/content", {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ volid: b.volid, node: b.node }),
        });
        g.backups = g.backups.filter(x => x.volid !== b.volid);
        g.count = g.backups.length;
        g.size = g.backups.reduce((a, x) => a + (x.size || 0), 0);
        g.latest = g.backups.reduce((a, x) => Math.max(a, x.ctime || 0), 0);
        this.recomputeTotals();
        ProxUtils.notify("Backup deleted.", "success");
      } catch (e) {
        ProxUtils.notify("Delete failed: " + e.message, "error");
      } finally {
        this.busyVolid = null;
      }
    },
  };
}
