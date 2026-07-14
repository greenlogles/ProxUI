// static/js/vm-editor.js — Alpine component for VM/CT configuration editor

export function vmEditor(node, vmid, vmType) {
  return {
    node, vmid, vmType,

    // form state (loaded from API in init)
    hostname: '',
    cpuType: 'host',
    sockets: 1,
    cores: 1,
    memory: 512,
    swap: 0,
    vgaType: 'std',
    vgaMemory: 16,
    autostart: false,

    // dynamic sections
    networks: [],     // [{key, model, bridge, mac, ipConfig, ipAddress, gateway, name}]
    disks: [],        // [{key, storage, size, sizeGB, isRootfs, displayName, displayType, resizeTo}]
    isos: [],         // [{key, isoPath, isoName}]
    cloudinit: null,  // {key, storage} | null
    bootDevices: [],  // [{id, type, name, icon, priority, enabled}]

    availableBridges: [],
    availableIsos: [],

    newNet: { bridge: '', model: 'virtio' },
    attachSel: { iso: '', iface: 'ide2' },

    loading: true,
    saving: false,
    supportsDrag: true,
    _sortable: null,
    _modals: {},

    vgaTypeNeedsMemory(type) {
      return type !== 'serial0' && type !== 'none';
    },

    init() {
      if (this.$refs.addNetworkModal) {
        this._modals.addNetworkModal = new bootstrap.Modal(this.$refs.addNetworkModal);
      }
      if (this.vmType === 'qemu' && this.$refs.attachIsoModal) {
        this._modals.attachIsoModal = new bootstrap.Modal(this.$refs.attachIsoModal);
      }
      this.loadConfig();
      this.loadAvailableBridges();
    },

    destroy() {
      Object.values(this._modals).forEach(m => m.dispose());
      this._sortable?.destroy();
    },

    async loadConfig() {
      this.loading = true;
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/config`);
        if (data.error) {
          window.ProxUtils.notify('Error loading config: ' + data.error, 'error');
          return;
        }

        this.autostart = data.onboot === 1;
        this.cores = data.cores || 1;
        this.memory = data.memory || 512;

        if (this.vmType === 'lxc') {
          this.hostname = data.hostname || '';
          this.swap = data.swap !== undefined ? data.swap : 0;
        } else {
          this.cpuType = data.cpu || 'host';
          this.sockets = data.sockets || 1;
          if (data.vga) {
            const vgaParts = data.vga.split(',');
            this.vgaType = vgaParts[0];
            const memPart = vgaParts.find(p => p.startsWith('memory='));
            this.vgaMemory = memPart ? parseInt(memPart.replace('memory=', '')) : 16;
          }
        }

        this.networks = this._parseNetworks(data);
        this.disks = this._parseDisks(data);

        if (this.vmType === 'qemu') {
          this.isos = this._parseIsos(data);
          this.cloudinit = this._parseCloudinit(data);
          this.bootDevices = this._parseBootOrder(data);
          await this.$nextTick();
          this.initBootSorting();
        }
      } catch (e) {
        window.ProxUtils.notify('Failed to load configuration: ' + e.message, 'error');
      } finally {
        this.loading = false;
      }
    },

    _parseNetworks(data) {
      const nets = [];
      for (const [key, value] of Object.entries(data)) {
        if (!key.match(/^net\d+$/)) continue;
        const parts = value.split(',');
        let model = 'virtio', bridge = '', mac = '', ipConfig = 'dhcp', ipAddress = '', gateway = '', name = 'eth0';
        for (const part of parts) {
          if (part.includes('=')) {
            const [k, v] = part.split('=', 2);
            if (['virtio', 'e1000', 'rtl8139', 'vmxnet3', 'ne2k_pci', 'ne2k_isa', 'pcnet', 'e1000e'].includes(k)) {
              model = k; mac = v;
            } else if (k === 'bridge') bridge = v;
            else if (k === 'ip') {
              if (v === 'dhcp') { ipConfig = 'dhcp'; } else { ipConfig = 'static'; ipAddress = v; }
            } else if (k === 'gw') gateway = v;
            else if (k === 'name') name = v;
          } else if (['virtio', 'e1000', 'rtl8139', 'vmxnet3'].includes(part)) {
            model = part;
          }
        }
        nets.push({ key, model, bridge, mac, ipConfig, ipAddress, gateway, name });
      }
      return nets.sort((a, b) => +a.key.replace('net', '') - +b.key.replace('net', ''));
    },

    _parseDisks(data) {
      const disks = [];
      if (this.vmType === 'lxc') {
        if (data.rootfs) disks.push(this._parseDiskEntry('rootfs', data.rootfs, true));
        for (const [key, value] of Object.entries(data)) {
          if (key.match(/^mp\d+$/)) disks.push(this._parseDiskEntry(key, value, false));
        }
      } else {
        for (const [key, value] of Object.entries(data)) {
          if (!key.match(/^(scsi|virtio|ide|sata)\d+$/)) continue;
          if (value.includes('.iso') || value.includes('media=cdrom') || value.toLowerCase().includes('cloudinit')) continue;
          disks.push(this._parseDiskEntry(key, value, false));
        }
        disks.sort((a, b) => {
          const at = a.key.replace(/\d+$/, ''), bt = b.key.replace(/\d+$/, '');
          if (at !== bt) return at.localeCompare(bt);
          return +a.key.replace(/^[a-z]+/, '') - +b.key.replace(/^[a-z]+/, '');
        });
      }
      return disks;
    },

    _parseDiskEntry(key, value, isRootfs) {
      const parts = value.split(',');
      let storage = '', size = '';
      const first = parts[0];
      if (first.includes(':')) {
        const cp = first.split(':');
        storage = cp[0];
        if (this.vmType === 'lxc' && cp[1] && !cp[1].includes('-')) size = cp[1] + 'G';
      }
      for (const p of parts) {
        if (p.startsWith('size=')) size = p.replace('size=', '');
      }
      let sizeGB = 0;
      if (size.endsWith('G')) sizeGB = parseInt(size.replace('G', ''));
      else if (size.endsWith('M')) sizeGB = Math.round(parseInt(size.replace('M', '')) / 1024 * 10) / 10;
      else if (size && !isNaN(parseInt(size))) { sizeGB = parseInt(size); size = size + 'G'; }
      return {
        key,
        storage,
        size: size || 'Unknown',
        sizeGB: sizeGB || 1,
        isRootfs,
        displayName: isRootfs ? 'Root Filesystem' : key,
        displayType: isRootfs ? 'rootfs' : (key.match(/^mp\d+$/) ? 'Mount Point' : storage),
        resizeTo: sizeGB || 1,
      };
    },

    _parseIsos(data) {
      const isos = [];
      for (const [key, value] of Object.entries(data)) {
        if (!key.match(/^ide\d+$/)) continue;
        if (!value.includes('.iso') && !value.includes('media=cdrom')) continue;
        let isoPath = '', isoName = '';
        for (const part of value.split(',')) {
          if (part.includes(':') && (part.includes('.iso') || part.includes('media=cdrom'))) {
            isoPath = part;
            const pathParts = part.split('/');
            isoName = pathParts[pathParts.length - 1].split(',')[0];
            break;
          }
        }
        isos.push({ key, isoPath, isoName });
      }
      return isos.sort((a, b) => +a.key.replace('ide', '') - +b.key.replace('ide', ''));
    },

    _parseCloudinit(data) {
      for (const [key, value] of Object.entries(data)) {
        if (!key.match(/^(scsi|ide|sata|virtio)\d+$/)) continue;
        if (!value.toLowerCase().includes('cloudinit')) continue;
        const parts = value.split(',');
        const storage = parts[0]?.includes(':') ? parts[0].split(':')[0] : '';
        return { key, storage };
      }
      return null;
    },

    _parseBootOrder(data) {
      const TYPES = {
        disk:    { icon: 'bi-hdd',      priority: 1 },
        cdrom:   { icon: 'bi-disc',     priority: 2 },
        network: { icon: 'bi-ethernet', priority: 3 },
      };
      const available = [];
      for (const [key, value] of Object.entries(data)) {
        if (!key.match(/^(scsi|virtio|ide|sata)\d+$/)) continue;
        if (value.includes('media=cdrom') || value.includes('.iso')) {
          available.push({ id: key, type: 'cdrom', name: `${key.toUpperCase()} (CD-ROM)`, ...TYPES.cdrom });
        } else if (!value.toLowerCase().includes('cloudinit')) {
          available.push({ id: key, type: 'disk', name: `${key.toUpperCase()} (Disk)`, ...TYPES.disk });
        }
      }
      available.push({ id: 'net', type: 'network', name: 'Network Boot (PXE)', ...TYPES.network });

      const boot = data.boot || '';
      if (boot.startsWith('order=')) {
        const order = boot.replace('order=', '').split(';');
        const ordered = order
          .map(id => available.find(d => d.id === id))
          .filter(Boolean)
          .map(d => ({ ...d, enabled: true }));
        available.forEach(d => {
          if (!ordered.find(o => o.id === d.id)) ordered.push({ ...d, enabled: false });
        });
        return ordered;
      }
      return available.sort((a, b) => a.priority - b.priority).map(d => ({ ...d, enabled: true }));
    },

    async loadAvailableBridges() {
      try {
        const data = await window.ProxUtils.apiJson(`/api/node/${this.node}/networks`);
        this.availableBridges = Array.isArray(data) ? data.filter(n => n.type === 'bridge') : [];
      } catch {
        this.availableBridges = [];
      }
    },

    // ── Network ────────────────────────────────────────────────────────────────

    openAddNetwork() {
      if (!this.availableBridges.length) {
        window.ProxUtils.notify('No network bridges available', 'warning');
        return;
      }
      this.newNet = { bridge: this.availableBridges[0]?.iface || '', model: 'virtio' };
      this._modals.addNetworkModal?.show();
    },

    async addNetwork() {
      if (!this.newNet.bridge) {
        window.ProxUtils.notify('Please select a network bridge', 'warning');
        return;
      }
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/config`);
        let nextId = 0;
        while (data[`net${nextId}`]) nextId++;
        const updateData = {};
        updateData[`net${nextId}`] = `${this.newNet.model},bridge=${this.newNet.bridge}`;
        const result = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/config`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(updateData),
        });
        this._modals.addNetworkModal?.hide();
        if (result.success) {
          window.ProxUtils.notify('Network interface added', 'success');
          await this.loadConfig();
        } else {
          window.ProxUtils.notify(result.error || 'Failed to add interface', 'error');
        }
      } catch (e) {
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
      }
    },

    async removeNetwork(key) {
      if (!confirm(`Remove network interface ${key}?`)) return;
      try {
        const updateData = {};
        updateData[key] = '';
        const result = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/config`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(updateData),
        });
        if (result.success) {
          window.ProxUtils.notify(`Interface ${key} removed`, 'success');
          await this.loadConfig();
        } else {
          window.ProxUtils.notify(result.error || 'Failed to remove', 'error');
        }
      } catch (e) {
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
      }
    },

    async onNetworkBridgeChange(iface) {
      if (!iface.bridge) return;
      if (this.vmType === 'lxc') {
        await this._saveLxcNetwork(iface);
      } else {
        await this._updateQemuNetworkBridge(iface);
      }
    },

    async _saveLxcNetwork(iface) {
      let cfg = `name=${iface.name || 'eth0'},bridge=${iface.bridge}`;
      if (iface.ipConfig === 'dhcp') {
        cfg += ',ip=dhcp';
      } else if (iface.ipConfig === 'static' && iface.ipAddress) {
        cfg += `,ip=${iface.ipAddress}`;
        if (iface.gateway) cfg += `,gw=${iface.gateway}`;
      }
      const updateData = {};
      updateData[iface.key] = cfg;
      try {
        const result = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/config`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(updateData),
        });
        if (result.success) window.ProxUtils.notify(`Interface ${iface.key} updated`, 'success');
        else { window.ProxUtils.notify(result.error || 'Failed to update', 'error'); await this.loadConfig(); }
      } catch (e) {
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
        await this.loadConfig();
      }
    },

    async _updateQemuNetworkBridge(iface) {
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/config`);
        const current = data[iface.key];
        if (!current) return;
        const parts = current.split(',');
        let found = false;
        for (let i = 0; i < parts.length; i++) {
          if (parts[i].startsWith('bridge=')) { parts[i] = `bridge=${iface.bridge}`; found = true; }
        }
        if (!found) parts.push(`bridge=${iface.bridge}`);
        const updateData = {};
        updateData[iface.key] = parts.join(',');
        const result = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/config`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(updateData),
        });
        if (result.success) window.ProxUtils.notify(`Interface ${iface.key} updated`, 'success');
        else { window.ProxUtils.notify(result.error || 'Failed to update', 'error'); await this.loadConfig(); }
      } catch (e) {
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
        await this.loadConfig();
      }
    },

    // ── Disks ──────────────────────────────────────────────────────────────────

    async resizeDisk(key) {
      const disk = this.disks.find(d => d.key === key);
      if (!disk) return;
      const newSize = +disk.resizeTo;
      if (!newSize || newSize <= 0) { window.ProxUtils.notify('Enter a valid size', 'warning'); return; }
      if (!confirm(`Resize disk ${key} to ${newSize}GB? This cannot be undone.`)) return;
      try {
        const result = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/resize-disk`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ disk: key, size: newSize }),
        });
        if (result.success) {
          window.ProxUtils.notify(`Disk ${key} resized to ${newSize}GB`, 'success');
          await this.loadConfig();
        } else {
          window.ProxUtils.notify(result.error || 'Failed to resize', 'error');
        }
      } catch (e) {
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
      }
    },

    // ── ISOs ───────────────────────────────────────────────────────────────────

    async openAttachIso() {
      try {
        const data = await window.ProxUtils.apiJson(`/api/node/${this.node}/iso-images`);
        if (!Array.isArray(data) || data.length === 0) {
          window.ProxUtils.notify('No ISO images available on this node', 'warning');
          return;
        }
        this.availableIsos = data;
        this.attachSel = { iso: '', iface: 'ide2' };
        this._modals.attachIsoModal?.show();
      } catch (e) {
        window.ProxUtils.notify('Error loading ISOs: ' + e.message, 'error');
      }
    },

    async attachIso() {
      if (!this.attachSel.iso) { window.ProxUtils.notify('Select an ISO image', 'warning'); return; }
      try {
        const result = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/iso/attach`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ iso_image: this.attachSel.iso, interface: this.attachSel.iface }),
        });
        this._modals.attachIsoModal?.hide();
        if (result.success) {
          window.ProxUtils.notify(result.message || 'ISO attached', 'success');
          await this.loadConfig();
        } else {
          window.ProxUtils.notify(result.error || 'Failed to attach ISO', 'error');
        }
      } catch (e) {
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
      }
    },

    async detachIso(key) {
      if (!confirm(`Detach ISO from ${key}?`)) return;
      try {
        const result = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/iso/detach`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ interface: key }),
        });
        if (result.success) {
          window.ProxUtils.notify(result.message || 'ISO detached', 'success');
          await this.loadConfig();
        } else {
          window.ProxUtils.notify(result.error || 'Failed to detach ISO', 'error');
        }
      } catch (e) {
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
      }
    },

    // ── Boot order ─────────────────────────────────────────────────────────────

    initBootSorting() {
      if (typeof Sortable === 'undefined' || !this.$refs.bootList) return;
      this.supportsDrag = 'draggable' in document.createElement('div');
      if (!this.supportsDrag) return;
      this._sortable?.destroy();
      this._sortable = new Sortable(this.$refs.bootList, {
        handle: '.drag-handle',
        ghostClass: 'sortable-ghost',
        chosenClass: 'sortable-chosen',
        animation: 150,
        onEnd: (evt) => {
          const moved = this.bootDevices.splice(evt.oldIndex, 1)[0];
          this.bootDevices.splice(evt.newIndex, 0, moved);
          this.saveBootOrder();
        },
      });
    },

    moveBoot(id, direction) {
      const idx = this.bootDevices.findIndex(d => d.id === id);
      if (idx < 0) return;
      if (direction === 'up' && idx > 0) {
        [this.bootDevices[idx - 1], this.bootDevices[idx]] = [this.bootDevices[idx], this.bootDevices[idx - 1]];
      } else if (direction === 'down' && idx < this.bootDevices.length - 1) {
        [this.bootDevices[idx], this.bootDevices[idx + 1]] = [this.bootDevices[idx + 1], this.bootDevices[idx]];
      }
      this.saveBootOrder();
    },

    async saveBootOrder() {
      const bootDeviceIds = this.bootDevices.filter(d => d.enabled).map(d => d.id);
      try {
        const result = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/boot-order`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ boot_devices: bootDeviceIds }),
        });
        if (result.success) {
          window.ProxUtils.notify('Boot order updated', 'success');
        } else {
          window.ProxUtils.notify(result.error || 'Failed to update boot order', 'error');
          await this.loadConfig();
        }
      } catch (e) {
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
        await this.loadConfig();
      }
    },

    // ── Save ───────────────────────────────────────────────────────────────────

    async save(redirect = false) {
      this.saving = true;
      try {
        const formData = {
          cores: this.cores,
          memory: this.memory,
          onboot: this.autostart ? 1 : 0,
        };
        if (this.vmType === 'lxc') {
          if (this.hostname) formData.hostname = this.hostname;
          formData.swap = this.swap || 0;
        } else {
          formData.cpu = this.cpuType;
          formData.sockets = this.sockets;
          if (this.vgaType === 'serial0' || this.vgaType === 'none') {
            formData.vga = this.vgaType;
          } else {
            formData.vga = this.vgaMemory
              ? `${this.vgaType},memory=${this.vgaMemory}`
              : this.vgaType;
          }
        }
        const result = await window.ProxUtils.apiJson(`/api/vm/${this.node}/${this.vmid}/config`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(formData),
        });
        if (result.success) {
          window.ProxUtils.notify('Configuration updated successfully', 'success');
          if (redirect) setTimeout(() => { window.location.href = `/vm/${this.node}/${this.vmid}`; }, 1000);
        } else {
          window.ProxUtils.notify(result.error || 'Failed to save configuration', 'error');
        }
      } catch (e) {
        window.ProxUtils.notify('Network error: ' + e.message, 'error');
      } finally {
        this.saving = false;
      }
    },
  };
}
