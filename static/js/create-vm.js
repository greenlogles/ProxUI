// static/js/create-vm.js — Alpine factory for the Create VM/Container form
export function createVmForm() {
  return {
    vmType: 'qemu',
    creationMethod: 'blank',
    node: '',
    storages: [],
    selectedStorage: '',
    bridges: [],
    selectedBridge: '',
    isos: [],
    selectedIso: '',
    templates: [],
    selectedTemplate: '',
    osTemplates: [],
    selectedOsTemplate: '',
    vmid: '',
    vmidMsg: '',
    vmidCls: '',
    vmidValid: null,
    _vmidTimer: null,

    init() {
      this.onTypeChange();
    },

    onTypeChange() {
      if (this.node) {
        this.loadStorages();
        this.loadTemplates();
        if (this.vmType === 'qemu') this.loadIsos();
      }
    },

    onNodeChange() {
      if (!this.node) return;
      this.loadStorages();
      this.loadBridges();
      this.loadTemplates();
      if (this.vmType === 'qemu') this.loadIsos();
      this.loadNextVmid();
    },

    onMethodChange() {
      if (this.creationMethod === 'template' && this.node) this.loadTemplates();
    },

    async loadStorages() {
      try {
        const data = await window.ProxUtils.apiJson(`/api/node/${this.node}/storages?vm_type=${this.vmType}`);
        if (data.error || !Array.isArray(data)) { this.storages = []; return; }
        this.storages = data.map(s => ({
          value: s.storage,
          label: `${s.storage} (${s.type})${s.available_gb !== undefined ? ` - ${s.available_gb} GB free` : ''}`,
          available_gb: s.available_gb ?? 0,
        }));
        const usable = this.storages.filter(s => s.available_gb >= 10);
        this.selectedStorage = (usable.length ? usable[0] : this.storages[0])?.value ?? '';
      } catch { this.storages = []; }
    },

    async loadBridges() {
      try {
        const data = await window.ProxUtils.apiJson(`/api/node/${this.node}/networks`);
        if (data.error || !Array.isArray(data)) { this.bridges = []; return; }
        this.bridges = data.map(n => ({ value: n.iface, label: n.iface }));
        this.selectedBridge = this.bridges[0]?.value ?? '';
      } catch { this.bridges = []; }
    },

    async loadTemplates() {
      try {
        const data = await window.ProxUtils.apiJson(`/api/node/${this.node}/templates`);
        if (data.error) { this.templates = []; this.osTemplates = []; return; }
        if (this.vmType === 'qemu') {
          this.templates = (data.qemu || []).map(t => ({ value: t.vmid, label: t.name || `Template VM ${t.vmid}` }));
        } else {
          this.osTemplates = (data.lxc || []).map(t => ({ value: t.volid, label: t.volid.split('/').pop() }));
        }
      } catch { this.templates = []; this.osTemplates = []; }
    },

    async loadIsos() {
      try {
        const data = await window.ProxUtils.apiJson(`/api/node/${this.node}/iso-images`);
        if (data.error || !Array.isArray(data)) { this.isos = []; return; }
        this.isos = data.map(iso => {
          const name = iso.volid.split('/').pop();
          const size = iso.size ? ` (${(iso.size / 1073741824).toFixed(1)} GB)` : '';
          return { value: iso.volid, label: name + size };
        });
      } catch { this.isos = []; }
    },

    async loadNextVmid() {
      try {
        const data = await window.ProxUtils.apiJson('/api/cluster/nextid');
        if (!data.error) {
          this.vmid = String(data.vmid);
          this.checkVmid();
        }
      } catch {}
    },

    scheduleVmidCheck() {
      clearTimeout(this._vmidTimer);
      this._vmidTimer = setTimeout(() => this.checkVmid(), 400);
    },

    async checkVmid() {
      const vmid = Number(this.vmid);
      if (!vmid || vmid < 100) {
        this.vmidMsg = '';
        this.vmidCls = '';
        this.vmidValid = null;
        return;
      }
      this.vmidMsg = 'Checking…';
      this.vmidCls = 'text-muted';
      this.vmidValid = null;
      try {
        const data = await window.ProxUtils.apiJson(`/api/cluster/vmid/${vmid}/check`);
        if (data.error) {
          this.vmidMsg = data.error;
          this.vmidCls = 'text-warning';
          this.vmidValid = null;
        } else if (data.available) {
          this.vmidMsg = 'VMID is available';
          this.vmidCls = 'text-success';
          this.vmidValid = true;
        } else {
          this.vmidMsg = data.reason + (data.name ? ` (${data.name} on ${data.node})` : '');
          this.vmidCls = 'text-danger';
          this.vmidValid = false;
        }
      } catch {
        this.vmidMsg = '';
        this.vmidValid = null;
      }
    },
  };
}
