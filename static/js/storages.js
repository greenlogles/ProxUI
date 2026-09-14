import * as ProxUtils from "./utils.js";

// Types ProxUI can add/edit/remove here. iscsi/iscsidirect/btrfs/esxi and
// ZFS-over-iSCSI are left out: they need target/LUN or vendor setup with no
// safe defaults to offer.
const CREATABLE_TYPES = ["nfs", "cifs", "dir", "pbs", "zfspool", "lvm", "lvmthin", "rbd", "cephfs"];

const TYPE_LABELS = {
  nfs: "NFS share", cifs: "SMB/CIFS share", dir: "Directory",
  pbs: "Proxmox Backup Server", zfspool: "ZFS pool",
  lvm: "LVM volume group", lvmthin: "LVM-thin pool",
  rbd: "Ceph RBD", cephfs: "CephFS",
};

const CONTENT_LABELS = {
  images: "Disk images", rootdir: "Container volumes", vztmpl: "Templates",
  iso: "ISO images", backup: "Backups", snippets: "Snippets",
};
const CONTENT_CHOICES = Object.keys(CONTENT_LABELS);

// What PVE actually allows per type -- offering the rest just invites a
// "content type not supported" rejection from the API.
const TYPE_CONTENT = {
  nfs: ["images", "rootdir", "vztmpl", "iso", "backup", "snippets"],
  cifs: ["images", "rootdir", "vztmpl", "iso", "backup", "snippets"],
  dir: ["images", "rootdir", "vztmpl", "iso", "backup", "snippets"],
  pbs: ["backup"],
  zfspool: ["images", "rootdir"],
  lvm: ["images", "rootdir"],
  lvmthin: ["images", "rootdir"],
  rbd: ["images", "rootdir"],
  cephfs: ["vztmpl", "iso", "backup", "snippets"],
};

const TYPE_DEFAULT_CONTENT = {
  nfs: ["images", "rootdir", "iso", "backup"],
  cifs: ["images", "rootdir", "iso", "backup"],
  dir: ["images", "rootdir"],
  pbs: ["backup"],
  zfspool: ["images", "rootdir"],
  lvm: ["images", "rootdir"],
  lvmthin: ["images", "rootdir"],
  rbd: ["images", "rootdir"],
  cephfs: ["vztmpl", "iso", "backup"],
};

// Block/local types are node-local unless the cluster shares them; network and
// Ceph types are shared by nature.
const TYPE_DEFAULT_SHARED = {
  nfs: true, cifs: true, pbs: true, rbd: true, cephfs: true,
  dir: false, zfspool: false, lvm: false, lvmthin: false,
};

// One-line "where does this storage live" summary, shown read-only in the edit
// form because none of these have a PUT parameter.
const LOCATION_OF = {
  nfs: s => `${s.server}:${s.export || ""}`,
  cifs: s => `//${s.server}/${s.share || ""}`,
  dir: s => s.path || "",
  pbs: s => `${s.server}:${s.datastore || ""}${s.namespace ? " ns:" + s.namespace : ""}`,
  zfspool: s => s.pool || "",
  lvm: s => s.vgname || "",
  lvmthin: s => `${s.vgname || ""}/${s.thinpool || ""}`,
  rbd: s => `${s.pool || ""}${s.monhost ? " @ " + s.monhost : ""}`,
  cephfs: s => `${s["fs-name"] || "cephfs"}${s.subdir ? s.subdir : ""}`,
};

function emptyForm(type = "nfs") {
  return {
    storage: "", type,
    content: (TYPE_DEFAULT_CONTENT[type] || ["images", "rootdir"]).slice(),
    nodes: [], disable: false, shared: !!TYPE_DEFAULT_SHARED[type],
    // nfs
    server: "", export: "", options: "",
    // cifs
    share: "", username: "", password: "", domain: "",
    // dir
    path: "", mkdir: true, is_mountpoint: false,
    // pbs
    datastore: "", fingerprint: "", namespace: "",
    // zfspool
    pool: "", sparse: true, blocksize: "", mountpoint: "",
    // lvm / lvmthin
    vgname: "", thinpool: "", base: "",
    // rbd / cephfs
    monhost: "", keyring: "", krbd: false, "fs-name": "", subdir: "",
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
    typeLabels: TYPE_LABELS,

    // Only the content types PVE accepts for the selected storage type.
    get typeContentChoices() {
      return TYPE_CONTENT[this.form.type] || CONTENT_CHOICES;
    },

    typeLabel(t) {
      return TYPE_LABELS[t] || t;
    },

    // Switching type in the create form resets the type-specific defaults;
    // anything already typed into a field of the old type is irrelevant.
    onTypeChange() {
      const keep = this.form.storage;
      this.form = { ...emptyForm(this.form.type), storage: keep };
    },

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

    // A non-shared storage of the same name exists once per node (local,
    // cloudinit, ...), so the name alone is not unique and a duplicate x-for
    // key makes Alpine bail out of the whole list.
    rowKey(s) {
      if (s.id) return s.id;
      const where = s.node || (s.nodes || []).join("-");
      return where ? `${where}/${s.storage}` : s.storage;
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
        datastore: s.datastore || "", fingerprint: s.fingerprint || "",
        namespace: s.namespace || "",
        pool: s.pool || "", sparse: !!s.sparse,
        blocksize: s.blocksize || "", mountpoint: s.mountpoint || "",
        vgname: s.vgname || "", thinpool: s.thinpool || "", base: s.base || "",
        // PVE never returns a stored keyring, so this is always a fresh entry.
        monhost: s.monhost || "", keyring: "", krbd: !!s.krbd,
        "fs-name": s["fs-name"] || "", subdir: s.subdir || "",
      };
      this.formLocation = LOCATION_OF[s.type] ? LOCATION_OF[s.type](s) : (s.path || "");
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
      } else if (f.type === "pbs") {
        body.server = f.server;
        body.username = f.username;
        if (f.password) body.password = f.password;
        body.fingerprint = f.fingerprint;
        body.namespace = f.namespace;
        if (this.creating) body.datastore = f.datastore;
      } else if (f.type === "zfspool") {
        body.sparse = f.sparse;
        body.blocksize = f.blocksize;
        body.mountpoint = f.mountpoint;
        if (this.creating) body.pool = f.pool;
      } else if (f.type === "lvm" || f.type === "lvmthin") {
        // vgname/thinpool/base have no PUT parameter -- the volume group a
        // storage points at is fixed once it exists.
        if (this.creating) {
          body.vgname = f.vgname;
          if (f.type === "lvmthin") body.thinpool = f.thinpool;
          else body.base = f.base;
        }
      } else if (f.type === "rbd") {
        body.monhost = f.monhost;
        body.username = f.username;
        body.namespace = f.namespace;
        if (f.keyring) body.keyring = f.keyring;
        body.krbd = f.krbd;
        if (this.creating) body.pool = f.pool;
      } else if (f.type === "cephfs") {
        body.monhost = f.monhost;
        body.username = f.username;
        body["fs-name"] = f["fs-name"];
        body.subdir = f.subdir;
        if (f.keyring) body.keyring = f.keyring;
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
