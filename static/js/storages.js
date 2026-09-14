import * as ProxUtils from "./utils.js";

// Types ProxUI can add/edit/remove here. pbs/zfs/lvm/ceph/etc need
// cluster-specific setup out of scope for the homelab NAS case.
const CREATABLE_TYPES = ["nfs", "cifs", "dir"];

const CONTENT_LABELS = {
  images: "Disk images", rootdir: "Container volumes", vztmpl: "Templates",
  iso: "ISO images", backup: "Backups", snippets: "Snippets",
};
const CONTENT_CHOICES = Object.keys(CONTENT_LABELS);

function emptyForm(type = "nfs") {
  return {
    storage: "", type,
    content: type === "dir" ? ["images", "rootdir"] : ["images", "rootdir", "iso", "backup"],
    nodes: [], disable: false, shared: type !== "dir",
    // nfs
    server: "", export: "", options: "",
    // cifs
    share: "", username: "", password: "", domain: "",
    // dir
    path: "", mkdir: true, is_mountpoint: false,
  };
}

// Storage page: cluster-wide add/edit/remove for nfs, cifs and dir storage,
// plus scan helpers to list NFS exports / CIFS shares off a chosen node.
export function storagesApp(initialStorages, nodeNames) {
  return {
    storages: initialStorages || [],
    nodeNames: nodeNames || [],
    search: "",
    loading: false,

    form: emptyForm(),
    creating: true,
    formError: "",
    saving: false,
    // Immutable once created; shown read-only in the edit modal.
    formLocation: "",
    _modals: {},

    // Scan helper state
    scanNode: "",
    scanning: false,
    scanError: "",
    scanResults: [],

    // Delete confirmation
    removeTarget: null,
    removeConfirm: "",
    removing: false,

    contentChoices: CONTENT_CHOICES,
    contentLabels: CONTENT_LABELS,
    creatableTypes: CREATABLE_TYPES,

    get visible() {
      const q = this.search.trim().toLowerCase();
      if (!q) return this.storages;
      return this.storages.filter(s =>
        [s.storage, this.pluginType(s), s.server, s.path].some(v => v && String(v).toLowerCase().includes(q)));
    },

    // /cluster/resources reports the resource kind ("storage") in `type` and
    // the plugin in `plugintype`; the node-by-node fallback has only `type`,
    // already holding the plugin name.
    pluginType(s) {
      return s.plugintype || s.type || "";
    },

    editable(s) {
      return CREATABLE_TYPES.includes(this.pluginType(s));
    },

    _modal(ref) {
      if (!this._modals[ref]) {
        this._modals[ref] = new bootstrap.Modal(this.$refs[ref]);
      }
      return this._modals[ref];
    },

    async reload() {
      this.loading = true;
      try {
        this.storages = await ProxUtils.apiJson("/api/storages");
      } catch (e) {
        ProxUtils.notify("Could not refresh storages: " + e.message, "error");
      } finally {
        this.loading = false;
      }
    },

    newStorage() {
      this.creating = true;
      this.form = emptyForm();
      this.formLocation = "";
      this.formError = "";
      this.scanResults = [];
      this.scanError = "";
      this.scanNode = this.nodeNames[0] || "";
      this._modal("storageModal").show();
    },

    // The overview row comes from cluster.resources (status only) — fetch
    // the real /storage config for anything the edit form needs.
    async editStorage(row) {
      this.creating = false;
      this.formError = "";
      this.scanResults = [];
      this.scanError = "";
      let s;
      try {
        s = await ProxUtils.apiJson(`/api/storages/${row.storage}`);
      } catch (e) {
        ProxUtils.notify("Could not load storage config: " + e.message, "error");
        return;
      }
      const content = (s.content || "").split(",").filter(Boolean);
      const nodes = (s.nodes || "").split(",").filter(Boolean);
      this.form = {
        ...emptyForm(s.type),
        storage: row.storage, type: s.type, content, nodes,
        disable: !!s.disable, shared: !!s.shared,
        server: s.server || "", options: s.options || "",
        username: s.username || "", password: "", domain: s.domain || "",
        mkdir: s.mkdir === undefined ? true : !!s.mkdir,
        is_mountpoint: !!s.is_mountpoint,
      };
      this.formLocation = s.type === "nfs" ? `${s.server}:${s.export || ""}`
        : s.type === "cifs" ? `//${s.server}/${s.share || ""}`
        : (s.path || "");
      this._modal("storageModal").show();
    },

    emptyFormFor(type) {
      return emptyForm(type);
    },

    humanizeBytes(bytes) {
      return ProxUtils.formatBytes(bytes);
    },

    toggleContent(c) {
      const i = this.form.content.indexOf(c);
      if (i === -1) this.form.content.push(c);
      else this.form.content.splice(i, 1);
    },

    toggleNode(n) {
      const i = this.form.nodes.indexOf(n);
      if (i === -1) this.form.nodes.push(n);
      else this.form.nodes.splice(i, 1);
    },

    async scan() {
      if (!this.scanNode || !this.form.server) return;
      this.scanning = true;
      this.scanError = "";
      this.scanResults = [];
      const url = this.form.type === "nfs" ? "/api/storage-scan/nfs" : "/api/storage-scan/cifs";
      const body = { node: this.scanNode, server: this.form.server };
      if (this.form.type === "cifs") {
        if (this.form.username) body.username = this.form.username;
        if (this.form.password) body.password = this.form.password;
        if (this.form.domain) body.domain = this.form.domain;
      }
      try {
        this.scanResults = await ProxUtils.apiJson(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch (e) {
        this.scanError = e.message;
      } finally {
        this.scanning = false;
      }
    },

    pickExport(r) {
      this.form.export = r.path;
    },

    pickShare(r) {
      this.form.share = r.share;
    },

    _payload() {
      const f = this.form;
      const body = {
        content: f.content, nodes: f.nodes,
        disable: f.disable, shared: f.shared,
      };
      if (f.type === "nfs") {
        body.server = f.server;
        body.options = f.options;
        if (this.creating) body.export = f.export;
      } else if (f.type === "cifs") {
        body.server = f.server;
        body.username = f.username;
        if (f.password) body.password = f.password;
        body.domain = f.domain;
        if (this.creating) body.share = f.share;
      } else if (f.type === "dir") {
        body.mkdir = f.mkdir;
        body.is_mountpoint = f.is_mountpoint;
        if (this.creating) body.path = f.path;
      }
      if (this.creating) {
        body.storage = f.storage;
        body.type = f.type;
      }
      return body;
    },

    async submit() {
      this.saving = true;
      this.formError = "";
      const url = this.creating ? "/api/storages" : `/api/storages/${this.form.storage}`;
      try {
        const r = await ProxUtils.apiJson(url, {
          method: this.creating ? "POST" : "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this._payload()),
        });
        this._modal("storageModal").hide();
        ProxUtils.notify(r.message, "success");
        await this.reload();
      } catch (e) {
        this.formError = e.message;
      } finally {
        this.saving = false;
      }
    },

    confirmRemove(s) {
      this.removeTarget = s;
      this.removeConfirm = "";
      this._modal("removeModal").show();
    },

    async remove() {
      if (!this.removeTarget || this.removeConfirm !== this.removeTarget.storage) return;
      this.removing = true;
      try {
        const r = await ProxUtils.apiJson(`/api/storages/${this.removeTarget.storage}`, {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: this.removeConfirm }),
        });
        this._modal("removeModal").hide();
        ProxUtils.notify(r.message, "success");
        await this.reload();
      } catch (e) {
        ProxUtils.notify(e.message, "error");
      } finally {
        this.removing = false;
      }
    },
  };
}
