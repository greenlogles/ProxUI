// static/js/isos.js — ISOs & Templates page Alpine component

export function isosManager() {
  return {
    nodes: [],
    loading: true,
    perNode: {},

    // Download ISO modal
    dl: {
      node: '', url: '', filename: '', storage: '', storages: [],
      loadingStorages: false, busy: false,
      showProgress: false, manualUrl: '',
    },

    // Cloud Template modal
    cloud: {
      node: '', image: '', name: '', vmid: '',
      cores: 2, memory: 2048, diskSize: '10G',
      storage: '', bridge: 'vmbr0',
      ciUser: '', ciPassword: '', ciSshKeys: '', ciIpConfig: 'ip=dhcp,ip6=auto',
      images: [], storages: [], bridges: [],
      vmidStatus: { checking: false, msg: '', valid: null },
      busy: false, showProgress: false, result: null,
    },

    // LXC Import modal
    lxc: {
      node: '', sourceTab: 'proxmox',
      lxcStorage: '', lxcUrlStorage: '', lxcOciStorage: '',
      storages: [], availableTemplates: {}, filterSection: '',
      selectedTemplate: '', templateData: null,
      lxcUrl: '', lxcUrlFilename: '', lxcOciImage: '',
      busy: false, showProgress: false, progressMsg: '', result: null,
    },

    // Create Container modal
    ct: {
      node: '', volid: '', templateInfo: '', hostname: '',
      vmid: '', vmidStatus: { checking: false, msg: '', valid: null },
      cores: 1, memory: 512, swap: 512, diskSize: 8,
      storage: '', storages: [], bridge: 'vmbr0', bridges: [],
      ipConfig: 'dhcp', ip: '', gateway: '',
      password: '', sshKeys: '', start: true, unprivileged: true,
      busy: false, showProgress: false, result: null,
    },

    _modals: {},

    // ── Computed ─────────────────────────────────────────────────────────────

    get cloudImageGroups() {
      const groups = { Ubuntu: [], Debian: [], 'RHEL-based': [], BSD: [], Other: [] };
      for (const img of this.cloud.images) {
        if (img.id.startsWith('ubuntu')) groups.Ubuntu.push(img);
        else if (img.id.startsWith('debian')) groups.Debian.push(img);
        else if (img.id.startsWith('rocky') || img.id.startsWith('alma') || img.id.startsWith('centos')) groups['RHEL-based'].push(img);
        else if (img.id.startsWith('freebsd') || img.id.startsWith('openbsd') || img.id.startsWith('netbsd')) groups.BSD.push(img);
        else groups.Other.push(img);
      }
      return Object.entries(groups)
        .filter(([, imgs]) => imgs.length > 0)
        .map(([label, images]) => ({ label, images }));
    },

    get cloudSelectedImage() {
      return this.cloud.images.find(i => i.id === this.cloud.image) || null;
    },

    get lxcSections() {
      return Object.keys(this.lxc.availableTemplates).sort().map(s => ({
        value: s,
        label: s.charAt(0).toUpperCase() + s.slice(1) + ` (${this.lxc.availableTemplates[s].length})`,
      }));
    },

    get lxcTemplateGroups() {
      const all = this.lxc.availableTemplates;
      const keys = this.lxc.filterSection
        ? [this.lxc.filterSection]
        : Object.keys(all).sort();
      return keys
        .map(s => ({ label: s.charAt(0).toUpperCase() + s.slice(1), templates: all[s] || [] }))
        .filter(g => g.templates.length > 0);
    },

    // ── Lifecycle ─────────────────────────────────────────────────────────────

    init() {
      ['downloadModal', 'cloudTemplateModal', 'lxcTemplateModal', 'createContainerModal'].forEach(r => {
        if (this.$refs[r]) this._modals[r] = new bootstrap.Modal(this.$refs[r]);
      });
      this.loadNodes();
    },

    destroy() {
      Object.values(this._modals).forEach(m => m.dispose());
    },

    // ── Node loading ──────────────────────────────────────────────────────────

    async loadNodes() {
      this.loading = true;
      try {
        const nodes = await window.ProxUtils.apiJson('/api/nodes');
        const online = (Array.isArray(nodes) ? nodes : [])
          .filter(n => n.status === 'online')
          .sort((a, b) => a.name.localeCompare(b.name));
        this.nodes = online;
        for (const node of online) {
          this.perNode[node.name] = {
            isos: [], vmTemplates: [], lxcTemplates: [],
            loadingIsos: true, loadingTemplates: true,
            isoError: '', templateError: '',
          };
        }
        this.loading = false;
        await Promise.all(online.map(n => this.loadNodeData(n.name)));
      } catch (e) {
        this.loading = false;
        window.ProxUtils.notify('Error loading nodes: ' + e.message, 'error');
      }
    },

    async loadNodeData(nodeName) {
      await Promise.all([this.loadIsos(nodeName), this.loadTemplates(nodeName)]);
    },

    async loadIsos(nodeName) {
      const nd = this.perNode[nodeName];
      if (!nd) return;
      nd.loadingIsos = true; nd.isoError = '';
      try {
        const data = await window.ProxUtils.apiJson(`/api/node/${nodeName}/iso-images`);
        if (data && data.error) { nd.isoError = data.error; nd.isos = []; }
        else nd.isos = Array.isArray(data) ? data : [];
      } catch (e) { nd.isoError = e.message; nd.isos = []; }
      finally { nd.loadingIsos = false; }
    },

    async loadTemplates(nodeName) {
      const nd = this.perNode[nodeName];
      if (!nd) return;
      nd.loadingTemplates = true; nd.templateError = '';
      try {
        const data = await window.ProxUtils.apiJson(`/api/node/${nodeName}/templates`);
        if (data && data.error) {
          nd.templateError = data.error; nd.vmTemplates = []; nd.lxcTemplates = [];
        } else {
          nd.vmTemplates = Array.isArray(data.qemu) ? data.qemu : [];
          nd.lxcTemplates = Array.isArray(data.lxc) ? data.lxc : [];
        }
      } catch (e) { nd.templateError = e.message; nd.vmTemplates = []; nd.lxcTemplates = []; }
      finally { nd.loadingTemplates = false; }
    },

    // ── Display helpers ───────────────────────────────────────────────────────

    isoFilename(volid) { return (volid || '').split('/').pop(); },
    isoStorage(volid) { return (volid || '').split('/')[0] || ''; },
    isoSizeGB(iso) { return iso.size ? (iso.size / 1073741824).toFixed(2) : 'Unknown'; },
    vmMemGB(vm) { return vm.maxmem ? (vm.maxmem / 1073741824).toFixed(1) : 'Unknown'; },
    vmCpuInfo(vm) { return vm.cpus ? `${vm.cpus} cores` : 'Unknown'; },
    lxcOsInfo(t) {
      const fn = (t.volid || '').split('/').pop();
      return fn.replace('.tar.xz', '').replace('.tar.gz', '').replace('.tar.zst', '');
    },
    lxcSizeGB(t) { return t.size ? (t.size / 1073741824).toFixed(2) : 'Unknown'; },
    storageLabel(s) { return `${s.storage} (${s.type}) - ${s.available_gb || 0} GB free`; },
    bridgeLabel(net) { return net.iface + (net.comments && net.comments !== 'default' ? ' - ' + net.comments : ''); },

    // ── Delete actions ────────────────────────────────────────────────────────

    async deleteIso(nodeName, volid) {
      const filename = this.isoFilename(volid);
      if (!confirm(`Delete ISO "${filename}"?\n\nThis cannot be undone.`)) return;
      try {
        const r = await window.ProxUtils.apiJson(`/api/node/${nodeName}/iso/${encodeURIComponent(volid)}`, { method: 'DELETE' });
        if (r.success) { window.ProxUtils.notify(`ISO "${filename}" deleted`, 'success'); await this.loadIsos(nodeName); }
        else window.ProxUtils.notify(r.error || 'Failed to delete ISO', 'error');
      } catch (e) { window.ProxUtils.notify('Error: ' + e.message, 'error'); }
    },

    async deleteVmTemplate(nodeName, vmid, name) {
      if (!confirm(`Delete VM template "${name}" (ID: ${vmid})?\n\nThis permanently deletes the template and all disks.`)) return;
      try {
        const r = await window.ProxUtils.apiJson(`/api/node/${nodeName}/template/${vmid}`, { method: 'DELETE' });
        if (r.success) { window.ProxUtils.notify(`Template "${name}" deleted`, 'success'); await this.loadTemplates(nodeName); }
        else window.ProxUtils.notify(r.error || 'Failed to delete template', 'error');
      } catch (e) { window.ProxUtils.notify('Error: ' + e.message, 'error'); }
    },

    async deleteLxcTemplate(nodeName, volid) {
      const filename = this.isoFilename(volid);
      if (!confirm(`Delete container template "${filename}"?\n\nThis cannot be undone.`)) return;
      try {
        const r = await window.ProxUtils.apiJson(`/api/node/${nodeName}/lxc-template/${encodeURIComponent(volid)}`, { method: 'DELETE' });
        if (r.success) { window.ProxUtils.notify(`Template "${filename}" deleted`, 'success'); await this.loadTemplates(nodeName); }
        else window.ProxUtils.notify(r.error || 'Failed to delete template', 'error');
      } catch (e) { window.ProxUtils.notify('Error: ' + e.message, 'error'); }
    },

    // ── Clipboard ─────────────────────────────────────────────────────────────

    async copyToClipboard(text) {
      try {
        await navigator.clipboard.writeText(text);
        window.ProxUtils.notify('Copied to clipboard!', 'success');
      } catch {
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); window.ProxUtils.notify('Copied!', 'success'); }
        catch { window.ProxUtils.notify('Failed to copy', 'error'); }
        document.body.removeChild(ta);
      }
    },

    // ── VMID availability check ───────────────────────────────────────────────

    async checkVmid(which) {
      const vmid = this[which].vmid;
      const status = this[which].vmidStatus;
      if (!vmid || vmid < 100) { status.checking = false; status.msg = ''; status.valid = null; return; }
      status.checking = true; status.msg = 'Checking...'; status.valid = null;
      try {
        const data = await window.ProxUtils.apiJson(`/api/cluster/vmid/${vmid}/check`);
        status.checking = false;
        if (data.error) { status.msg = data.error; status.valid = null; }
        else if (data.available) { status.msg = 'ID is available'; status.valid = true; }
        else {
          let msg = data.reason || 'ID in use';
          if (data.name) msg += ` (${data.name} on ${data.node})`;
          status.msg = msg; status.valid = false;
        }
      } catch { status.checking = false; status.msg = ''; status.valid = null; }
    },

    // ── Download ISO modal ────────────────────────────────────────────────────

    async openDownload(nodeName) {
      Object.assign(this.dl, {
        node: nodeName, url: '', filename: '', storage: '', storages: [],
        busy: false, showProgress: false, manualUrl: '', loadingStorages: true,
      });
      this._modals.downloadModal.show();
      try {
        const storages = await window.ProxUtils.apiJson(`/api/node/${nodeName}/iso-storages`);
        if (storages && storages.error) window.ProxUtils.notify('Error loading storages: ' + storages.error, 'error');
        else if (!Array.isArray(storages) || storages.length === 0) window.ProxUtils.notify('No ISO-compatible storages found', 'warning');
        else this.dl.storages = storages;
      } catch (e) { window.ProxUtils.notify('Error: ' + e.message, 'error'); }
      this.dl.loadingStorages = false;
    },

    dlAutofillFilename() {
      if (this.dl.url && !this.dl.filename) {
        const fn = this.dl.url.split('/').pop();
        if (fn && fn.includes('.')) this.dl.filename = fn;
      }
    },

    async startDownload() {
      if (!this.dl.url) { window.ProxUtils.notify('Please enter a URL', 'warning'); return; }
      if (!this.dl.storage) { window.ProxUtils.notify('Please select a storage', 'warning'); return; }
      this.dl.busy = true; this.dl.showProgress = false; this.dl.manualUrl = '';
      try {
        const result = await window.ProxUtils.apiJson(`/api/node/${this.dl.node}/download-iso`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: this.dl.url, storage: this.dl.storage, filename: this.dl.filename || undefined }),
        });
        if (result.success) {
          this.dl.showProgress = true;
          window.ProxUtils.notify(result.message, 'success');
          setTimeout(() => this.loadIsos(this.dl.node), 2000);
          setTimeout(() => this._modals.downloadModal.hide(), 3000);
        } else if (result.manual_instructions) {
          this.dl.manualUrl = this.dl.url;
          this.dl.showProgress = true;
          window.ProxUtils.notify('Manual download required', 'warning');
        } else {
          window.ProxUtils.notify(result.error || 'Download failed', 'error');
        }
      } catch (e) { window.ProxUtils.notify('Network error: ' + e.message, 'error'); }
      this.dl.busy = false;
    },

    // ── Cloud Template modal ──────────────────────────────────────────────────

    async openCloud(nodeName) {
      Object.assign(this.cloud, {
        node: nodeName, image: '', name: '', vmid: '',
        cores: 2, memory: 2048, diskSize: '10G',
        storage: '', bridge: 'vmbr0',
        ciUser: '', ciPassword: '', ciSshKeys: '', ciIpConfig: 'ip=dhcp,ip6=auto',
        storages: [], bridges: [],
        vmidStatus: { checking: false, msg: '', valid: null },
        busy: false, showProgress: false, result: null,
      });
      this._modals.cloudTemplateModal.show();
      await Promise.all([
        this.loadCloudImages(),
        this.loadCloudStorages(nodeName),
        this.loadCloudBridges(nodeName),
      ]);
    },

    async loadCloudImages() {
      if (this.cloud.images.length > 0) return;
      try {
        this.cloud.images = await window.ProxUtils.apiJson('/api/cloud-images');
      } catch (e) { window.ProxUtils.notify('Error loading cloud images: ' + e.message, 'error'); }
    },

    onCloudImageChange() {
      if (this.cloud.image && !this.cloud.name) {
        this.cloud.name = this.cloud.image + '-template';
      }
    },

    async loadCloudStorages(nodeName) {
      try {
        const storages = await window.ProxUtils.apiJson(`/api/node/${nodeName}/storages?vm_type=qemu`);
        this.cloud.storages = (storages && !storages.error && Array.isArray(storages)) ? storages : [];
      } catch { this.cloud.storages = []; }
    },

    async loadCloudBridges(nodeName) {
      try {
        const networks = await window.ProxUtils.apiJson(`/api/node/${nodeName}/networks`);
        if (!networks || networks.error) { this.cloud.bridges = [{ iface: 'vmbr0', comments: '' }]; return; }
        this.cloud.bridges = [
          { iface: 'vmbr0', comments: '' },
          ...networks.filter(n => n.iface && n.iface !== 'vmbr0'),
        ];
      } catch { this.cloud.bridges = [{ iface: 'vmbr0', comments: '' }]; }
    },

    async createCloudTemplate() {
      if (!this.cloud.image) { window.ProxUtils.notify('Please select a cloud image', 'warning'); return; }
      if (!this.cloud.storage) { window.ProxUtils.notify('Please select a storage', 'warning'); return; }
      this.cloud.busy = true; this.cloud.showProgress = true; this.cloud.result = null;
      try {
        const result = await window.ProxUtils.apiJson(`/api/node/${this.cloud.node}/create-cloud-template`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            image_id: this.cloud.image,
            storage: this.cloud.storage,
            name: this.cloud.name || undefined,
            vmid: this.cloud.vmid || undefined,
            cores: this.cloud.cores || 2,
            memory: this.cloud.memory || 2048,
            disk_size: this.cloud.diskSize || '10G',
            bridge: this.cloud.bridge || 'vmbr0',
            ci_user: this.cloud.ciUser || undefined,
            ci_password: this.cloud.ciPassword || undefined,
            ci_sshkeys: this.cloud.ciSshKeys || undefined,
            ip_config: this.cloud.ciIpConfig || 'ip=dhcp,ip6=auto',
          }),
        });
        this.cloud.showProgress = false;
        this.cloud.result = result;
        if (result.success) {
          window.ProxUtils.notify('Template creation job started', 'success');
          window.dispatchEvent(new CustomEvent('proxui:jobs-refresh'));
          setTimeout(() => this._modals.cloudTemplateModal.hide(), 3000);
        } else {
          window.ProxUtils.notify('Failed: ' + (result.error || 'Unknown error'), 'error');
        }
      } catch (e) {
        this.cloud.showProgress = false;
        this.cloud.result = { success: false, error: e.message };
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
      }
      this.cloud.busy = false;
    },

    // ── LXC Import modal ──────────────────────────────────────────────────────

    async openLxcImport(nodeName) {
      Object.assign(this.lxc, {
        node: nodeName, sourceTab: 'proxmox',
        lxcStorage: '', lxcUrlStorage: '', lxcOciStorage: '',
        storages: [], availableTemplates: {}, filterSection: '',
        selectedTemplate: '', templateData: null,
        lxcUrl: '', lxcUrlFilename: '', lxcOciImage: '',
        busy: false, showProgress: false, progressMsg: '', result: null,
      });
      this._modals.lxcTemplateModal.show();
      await Promise.all([this.loadLxcStorages(nodeName), this.loadAvailableLxcTemplates(nodeName)]);
    },

    async loadLxcStorages(nodeName) {
      try {
        const storages = await window.ProxUtils.apiJson(`/api/node/${nodeName}/lxc-storages`);
        if (storages && storages.error) {
          window.ProxUtils.notify('Error loading storages: ' + storages.error, 'error');
          this.lxc.storages = [];
        } else if (!Array.isArray(storages) || storages.length === 0) {
          window.ProxUtils.notify('No compatible storages found', 'warning');
          this.lxc.storages = [];
        } else {
          this.lxc.storages = storages;
        }
      } catch (e) { this.lxc.storages = []; window.ProxUtils.notify('Error: ' + e.message, 'error'); }
    },

    async loadAvailableLxcTemplates(nodeName) {
      try {
        const data = await window.ProxUtils.apiJson(`/api/node/${nodeName}/available-lxc-templates`);
        if (data && data.error) window.ProxUtils.notify('Error loading templates: ' + data.error, 'error');
        else this.lxc.availableTemplates = (data && data.templates) ? data.templates : {};
      } catch (e) { window.ProxUtils.notify('Error loading templates: ' + e.message, 'error'); }
    },

    onLxcTemplateChange() {
      const allTemplates = Object.values(this.lxc.availableTemplates).flat();
      this.lxc.templateData = allTemplates.find(t => t.template === this.lxc.selectedTemplate) || null;
    },

    lxcUrlAutofill() {
      if (this.lxc.lxcUrl && !this.lxc.lxcUrlFilename) {
        const fn = this.lxc.lxcUrl.split('/').pop().split('?')[0];
        if (fn && (fn.endsWith('.tar.xz') || fn.endsWith('.tar.gz') || fn.endsWith('.tar.zst'))) {
          this.lxc.lxcUrlFilename = fn;
        }
      }
    },

    async importLxcTemplate() {
      const src = this.lxc.sourceTab;
      let requestData;
      if (src === 'proxmox') {
        if (!this.lxc.lxcStorage) { window.ProxUtils.notify('Please select a storage', 'warning'); return; }
        if (!this.lxc.selectedTemplate) { window.ProxUtils.notify('Please select a template', 'warning'); return; }
        requestData = { storage: this.lxc.lxcStorage, source_type: 'proxmox', template: this.lxc.selectedTemplate };
      } else if (src === 'url') {
        if (!this.lxc.lxcUrlStorage) { window.ProxUtils.notify('Please select a storage', 'warning'); return; }
        if (!this.lxc.lxcUrl) { window.ProxUtils.notify('Please enter a URL', 'warning'); return; }
        requestData = { storage: this.lxc.lxcUrlStorage, source_type: 'url', url: this.lxc.lxcUrl, filename: this.lxc.lxcUrlFilename || undefined };
      } else {
        if (!this.lxc.lxcOciStorage) { window.ProxUtils.notify('Please select a storage', 'warning'); return; }
        if (!this.lxc.lxcOciImage) { window.ProxUtils.notify('Please enter an OCI image reference', 'warning'); return; }
        requestData = { storage: this.lxc.lxcOciStorage, source_type: 'oci', image: this.lxc.lxcOciImage };
      }
      this.lxc.busy = true; this.lxc.showProgress = true;
      this.lxc.progressMsg = 'Starting download...'; this.lxc.result = null;
      try {
        const result = await window.ProxUtils.apiJson(`/api/node/${this.lxc.node}/download-lxc-template`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requestData),
        });
        this.lxc.showProgress = false;
        this.lxc.result = result;
        if (result.success) {
          window.ProxUtils.notify(result.message, 'success');
          window.dispatchEvent(new CustomEvent('proxui:jobs-refresh'));
          setTimeout(() => this._modals.lxcTemplateModal.hide(), 3000);
          setTimeout(() => this.loadTemplates(this.lxc.node), 5000);
        } else if (!result.manual_command) {
          window.ProxUtils.notify(result.error || 'Import failed', 'error');
        }
      } catch (e) {
        this.lxc.showProgress = false;
        this.lxc.result = { success: false, error: e.message };
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
      }
      this.lxc.busy = false;
    },

    // ── Create Container modal ────────────────────────────────────────────────

    async openCreateCt(nodeName, volid) {
      const filename = volid.split('/').pop();
      const baseName = filename.replace('.tar.xz','').replace('.tar.gz','').replace('.tar.zst','').replace('.tar','');
      const safeName = baseName.replace(/[^a-zA-Z0-9\-]/g, '-').toLowerCase().substring(0, 20);
      Object.assign(this.ct, {
        node: nodeName, volid, templateInfo: filename, hostname: safeName,
        vmid: '', vmidStatus: { checking: false, msg: '', valid: null },
        cores: 1, memory: 512, swap: 512, diskSize: 8,
        storage: '', storages: [], bridge: 'vmbr0', bridges: [],
        ipConfig: 'dhcp', ip: '', gateway: '',
        password: '', sshKeys: '', start: true, unprivileged: true,
        busy: false, showProgress: false, result: null,
      });
      this._modals.createContainerModal.show();
      await Promise.all([this.loadCtStorages(nodeName), this.loadCtBridges(nodeName)]);
    },

    async loadCtStorages(nodeName) {
      try {
        const storages = await window.ProxUtils.apiJson(`/api/node/${nodeName}/storages?vm_type=lxc`);
        this.ct.storages = (storages && !storages.error && Array.isArray(storages)) ? storages : [];
      } catch { this.ct.storages = []; }
    },

    async loadCtBridges(nodeName) {
      try {
        const networks = await window.ProxUtils.apiJson(`/api/node/${nodeName}/networks`);
        if (!networks || networks.error) { this.ct.bridges = [{ iface: 'vmbr0', comments: '' }]; return; }
        this.ct.bridges = [
          { iface: 'vmbr0', comments: '' },
          ...networks.filter(n => n.iface && n.iface !== 'vmbr0'),
        ];
      } catch { this.ct.bridges = [{ iface: 'vmbr0', comments: '' }]; }
    },

    async createContainer() {
      const hostname = (this.ct.hostname || '').trim();
      if (!hostname) { window.ProxUtils.notify('Please enter a hostname', 'warning'); return; }
      if (!this.ct.storage) { window.ProxUtils.notify('Please select a storage', 'warning'); return; }
      const bridge = this.ct.bridge || 'vmbr0';
      let net0;
      if (this.ct.ipConfig === 'dhcp') {
        net0 = `name=eth0,bridge=${bridge},ip=dhcp`;
      } else {
        const ip = (this.ct.ip || '').trim();
        const gw = (this.ct.gateway || '').trim();
        net0 = ip
          ? `name=eth0,bridge=${bridge},ip=${ip}${gw ? ',gw=' + gw : ''}`
          : `name=eth0,bridge=${bridge},ip=dhcp`;
      }
      const data = {
        ostemplate: this.ct.volid, hostname, storage: this.ct.storage,
        rootfs_size: this.ct.diskSize || 8,
        cores: this.ct.cores || 1, memory: this.ct.memory || 512, swap: this.ct.swap || 512,
        unprivileged: this.ct.unprivileged, start: this.ct.start, net0,
      };
      if (this.ct.vmid) data.vmid = parseInt(this.ct.vmid);
      if (this.ct.password) data.password = this.ct.password;
      if ((this.ct.sshKeys || '').trim()) data.ssh_public_keys = this.ct.sshKeys.trim();
      this.ct.busy = true; this.ct.showProgress = true; this.ct.result = null;
      try {
        const result = await window.ProxUtils.apiJson(`/api/node/${this.ct.node}/lxc`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(data),
        });
        this.ct.showProgress = false;
        this.ct.result = result;
        if (result.success) {
          window.ProxUtils.notify(`Container ${result.vmid} created successfully!`, 'success');
          setTimeout(() => this._modals.createContainerModal.hide(), 3000);
        } else {
          window.ProxUtils.notify(result.error || 'Failed to create container', 'error');
        }
      } catch (e) {
        this.ct.showProgress = false;
        this.ct.result = { success: false, error: e.message };
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
      }
      this.ct.busy = false;
    },
  };
}
