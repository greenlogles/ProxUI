import * as ProxUtils from "./utils.js";

// Types ProxUI can create and remove. Physical NICs (eth) can be given an
// address but are not ours to delete — they come from the hardware, not from
// /etc/network/interfaces.
const CREATABLE = ["bridge", "bond", "vlan"];

// Which of a node's own interfaces make sense as a value for each field. The
// pickers only suggest — every field stays free-text, since an interface can be
// referenced before the node reports it (a bond slave named in the same edit,
// a NIC that is down, a name the kernel has not brought up yet).
const CANDIDATE_TYPES = {
  bridge_ports: ["eth", "bond", "vlan", "unknown"],
  slaves: ["eth", "unknown"],
  "vlan-raw-device": ["eth", "bond", "bridge", "unknown"],
};

const BOND_MODES = [
  "balance-rr", "active-backup", "balance-xor", "broadcast",
  "802.3ad", "balance-tlb", "balance-alb",
];

function emptyForm(type = "bridge") {
  return {
    iface: "", type, autostart: true, comments: "",
    cidr: "", gateway: "", cidr6: "", gateway6: "", mtu: "",
    bridge_ports: "", bridge_vlan_aware: false, bridge_vids: "",
    slaves: "", bond_mode: "balance-rr", bond_xmit_hash_policy: "", "bond-primary": "",
    "vlan-raw-device": "", "vlan-id": "",
  };
}

/** "192.168.1.5" + "255.255.255.0" or "24" → "192.168.1.5/24". */
function toCidr(address, netmask) {
  if (!address) return "";
  if (netmask == null || netmask === "") return address;
  const mask = String(netmask);
  if (!mask.includes(".")) return `${address}/${mask}`;
  const bits = mask.split(".")
    .map(o => (Number(o) >>> 0).toString(2).replace(/0/g, "").length)
    .reduce((a, b) => a + b, 0);
  return `${address}/${bits}`;
}

// Networks page: per-node interface inventory with create/edit/delete against
// /etc/network/interfaces.new, plus the apply and revert that Proxmox requires
// before any of it takes effect.
export function networksApp(initialNodes) {
  return {
    nodes: initialNodes || [],
    search: "",
    typeFilter: "",
    loading: false,
    busy: null,

    form: emptyForm(),
    formNode: "",
    formIface: "",
    formInterfaces: [],
    // Once the name is typed by hand, stop deriving it from the VLAN fields.
    nameTouched: false,
    formError: "",
    saving: false,
    creating: true,
    _modals: {},

    bondModes: BOND_MODES,

    // Node whose pending diff is open in the review modal.
    diffNode: null,

    get totalInterfaces() {
      return this.nodes.reduce((a, n) => a + n.interfaces.length, 0);
    },
    get pendingNodes() {
      return this.nodes.filter(n => n.pending);
    },
    get types() {
      const seen = new Set();
      for (const n of this.nodes) for (const i of n.interfaces) seen.add(i.type);
      return [...seen].sort();
    },

    visible(node) {
      const q = this.search.trim().toLowerCase();
      return node.interfaces.filter(i => {
        if (this.typeFilter && i.type !== this.typeFilter) return false;
        if (!q) return true;
        return [i.iface, i.type, i.cidr, i.address, i.bridge_ports, i.slaves, i.comments]
          .some(v => v && String(v).toLowerCase().includes(q));
      });
    },

    canDelete(node, i) {
      return CREATABLE.includes(i.type) && i.iface !== node.management;
    },

    /** Why the delete button is off, since a disabled button explains nothing. */
    deleteHint(node, i) {
      if (i.iface === node.management) {
        return `${i.iface} carries the address ${node.node} is reached on — ` +
          "deleting it would cut the node off";
      }
      if (!CREATABLE.includes(i.type)) {
        return `${i.type} interfaces come from the hardware, not from ` +
          "/etc/network/interfaces — there is nothing to delete";
      }
      return `Delete ${i.iface}`;
    },

    address(i) {
      return i.cidr || toCidr(i.address, i.netmask) || "—";
    },

    ports(i) {
      return i.bridge_ports || i.slaves ||
        (i["vlan-raw-device"] ? `${i["vlan-raw-device"]} (VLAN ${i["vlan-id"] || "?"})` : "") || "—";
    },

    async reload() {
      this.loading = true;
      try {
        const r = await ProxUtils.apiJson("/api/networks");
        this.nodes = r.nodes || [];
      } catch (e) {
        ProxUtils.notify("Could not refresh networks: " + e.message, "error");
      } finally {
        this.loading = false;
      }
    },

    _modal(ref) {
      if (!this._modals[ref]) {
        this._modals[ref] = new bootstrap.Modal(this.$refs[ref]);
      }
      return this._modals[ref];
    },

    newInterface(node, type = "bridge") {
      this.creating = true;
      this.formNode = node.node;
      this.formIface = "";
      this.formInterfaces = node.interfaces;
      this.form = emptyForm(type);
      this.nameTouched = false;
      this.formError = "";
      this._modal("ifaceModal").show();
    },

    editInterface(node, i) {
      this.creating = false;
      this.formNode = node.node;
      this.formIface = i.iface;
      this.formInterfaces = node.interfaces;
      this.form = {
        ...emptyForm(i.type),
        iface: i.iface,
        type: i.type,
        autostart: !!i.autostart,
        comments: (i.comments || "").trim(),
        cidr: i.cidr || toCidr(i.address, i.netmask),
        gateway: i.gateway || "",
        cidr6: i.cidr6 || toCidr(i.address6, i.netmask6),
        gateway6: i.gateway6 || "",
        mtu: i.mtu || "",
        bridge_ports: i.bridge_ports || "",
        bridge_vlan_aware: !!i.bridge_vlan_aware,
        bridge_vids: i.bridge_vids || "",
        slaves: i.slaves || "",
        bond_mode: i.bond_mode || "balance-rr",
        bond_xmit_hash_policy: i.bond_xmit_hash_policy || "",
        "bond-primary": i["bond-primary"] || "",
        "vlan-raw-device": i["vlan-raw-device"] || "",
        "vlan-id": i["vlan-id"] || "",
      };
      this.formError = "";
      this._modal("ifaceModal").show();
    },

    /** Name a new VLAN after its raw device and tag, the ifupdown2 convention. */
    syncVlanName() {
      if (!this.creating || this.form.type !== "vlan" || this.nameTouched) return;
      const raw = this.form["vlan-raw-device"].trim();
      const tag = String(this.form["vlan-id"]).trim();
      this.form.iface = raw && tag ? `${raw}.${tag}` : "";
    },

    /** A derived name belongs to the type it was derived for, so drop it. */
    onTypeChange() {
      if (!this.nameTouched) this.form.iface = "";
      this.syncVlanName();
    },

    /** Flag a name collision here rather than letting the API reject it. */
    nameExists() {
      return this.creating &&
        this.formInterfaces.some(i => i.iface === this.form.iface.trim());
    },

    /** Interfaces on this node that could plausibly fill the given field. */
    candidates(field) {
      const types = CANDIDATE_TYPES[field] || [];
      return this.formInterfaces
        .filter(i => types.includes(i.type) && i.iface !== this.form.iface)
        .map(i => i.iface);
    },

    /** A bond's primary has to be one of its own slaves. */
    primaryCandidates() {
      return this.members("slaves");
    },

    members(field) {
      return String(this.form[field] || "").split(/\s+/).filter(Boolean);
    },

    isMember(field, name) {
      return this.members(field).includes(name);
    },

    /** Click-to-toggle for the space-separated fields (ports, slaves). */
    toggleMember(field, name) {
      const current = this.members(field);
      const next = current.includes(name)
        ? current.filter(n => n !== name)
        : [...current, name];
      this.form[field] = next.join(" ");
    },

    /** The interface already claiming this NIC, so the picker can flag it. */
    claimedBy(name) {
      const owner = this.formInterfaces.find(i =>
        i.iface !== this.formIface &&
        [i.bridge_ports, i.slaves].some(v =>
          String(v || "").split(/\s+/).filter(Boolean).includes(name)));
      return owner ? owner.iface : "";
    },

    /** Drop the fields that don't belong to the chosen type before sending. */
    _payload() {
      const f = this.form;
      const body = {
        iface: f.iface, type: f.type, autostart: f.autostart,
        comments: f.comments, cidr: f.cidr, gateway: f.gateway,
        cidr6: f.cidr6, gateway6: f.gateway6, mtu: f.mtu,
      };
      if (f.type === "bridge") {
        body.bridge_ports = f.bridge_ports;
        body.bridge_vlan_aware = f.bridge_vlan_aware;
        if (f.bridge_vlan_aware) body.bridge_vids = f.bridge_vids;
      } else if (f.type === "bond") {
        body.slaves = f.slaves;
        body.bond_mode = f.bond_mode;
        if (["balance-xor", "802.3ad"].includes(f.bond_mode)) {
          body.bond_xmit_hash_policy = f.bond_xmit_hash_policy;
        }
        if (f.bond_mode === "active-backup") body["bond-primary"] = f["bond-primary"];
      } else if (f.type === "vlan") {
        body["vlan-raw-device"] = f["vlan-raw-device"];
        body["vlan-id"] = f["vlan-id"];
      }
      return body;
    },

    async submit() {
      this.saving = true;
      this.formError = "";
      const url = this.creating
        ? `/api/network/${this.formNode}`
        : `/api/network/${this.formNode}/${this.formIface}`;
      try {
        const r = await ProxUtils.apiJson(url, {
          method: this.creating ? "POST" : "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(this._payload()),
        });
        this._modal("ifaceModal").hide();
        ProxUtils.notify(r.message, "success");
        await this.reload();
      } catch (e) {
        this.formError = e.message;
      } finally {
        this.saving = false;
      }
    },

    async remove(node, i) {
      if (!confirm(`Delete interface ${i.iface} on ${node.node}?\n\n` +
        "The interface is removed from the pending configuration; it stays up " +
        "until you apply the changes.")) return;
      this.busy = `${node.node}/${i.iface}`;
      try {
        const r = await ProxUtils.apiJson(`/api/network/${node.node}/${i.iface}`, { method: "DELETE" });
        ProxUtils.notify(r.message, "success");
        await this.reload();
      } catch (e) {
        ProxUtils.notify(e.message, "error");
      } finally {
        this.busy = null;
      }
    },

    /** Open the pending diff so the change is read before it is committed. */
    review(node) {
      this.diffNode = node;
      this._modal("diffModal").show();
    },

    /** Proxmox' diff as lines tagged for colouring; [] when it gave us none. */
    get diffLines() {
      const raw = this.diffNode && this.diffNode.changes;
      if (!raw) return [];
      return raw.replace(/\n$/, "").split("\n").map(text => {
        let kind = "ctx";
        if (text.startsWith("+++") || text.startsWith("---")) kind = "file";
        else if (text.startsWith("@@")) kind = "hunk";
        else if (text.startsWith("+")) kind = "add";
        else if (text.startsWith("-")) kind = "del";
        return { text, kind };
      });
    },

    get diffStat() {
      const lines = this.diffLines;
      return {
        added: lines.filter(l => l.kind === "add").length,
        removed: lines.filter(l => l.kind === "del").length,
      };
    },

    async apply(node) {
      this._modal("diffModal").hide();
      this.busy = node.node;
      try {
        const r = await ProxUtils.apiJson(`/api/network/${node.node}/apply`, { method: "POST" });
        ProxUtils.notify(r.message, "success");
        await this.reload();
      } catch (e) {
        ProxUtils.notify("Apply failed: " + e.message, "error");
      } finally {
        this.busy = null;
      }
    },

    async revert(node) {
      if (!confirm(`Discard all pending network changes on ${node.node}?`)) return;
      this._modal("diffModal").hide();
      this.busy = node.node;
      try {
        const r = await ProxUtils.apiJson(`/api/network/${node.node}/revert`, { method: "POST" });
        ProxUtils.notify(r.message, "success");
        await this.reload();
      } catch (e) {
        ProxUtils.notify("Revert failed: " + e.message, "error");
      } finally {
        this.busy = null;
      }
    },
  };
}
