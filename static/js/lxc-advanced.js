// static/js/lxc-advanced.js — Alpine component for LXC Advanced / Passthrough

const LXC_FLAGS = [
  { key: 'nesting', label: 'Nesting', desc: 'Docker-in-LXC, systemd, nested LXC' },
  { key: 'keyctl',  label: 'Keyctl',  desc: 'Key retention service (needed by some apps)' },
  { key: 'fuse',    label: 'FUSE',    desc: 'User-space filesystem support' },
  { key: 'mount',   label: 'Mount',   desc: 'Mount arbitrary filesystems (cifs, nfs…)' },
  { key: 'mknod',   label: 'Mknod',   desc: 'Allow mknod in unprivileged containers' },
];

const QUICKSYNC_DEVICES = [
  { label: 'renderD128 (QuickSync / VA-API)', path: '/dev/dri/renderD128', gid: '104', uid: '0' },
  { label: 'card0 (DRI primary)',              path: '/dev/dri/card0',      gid: '44',  uid: '0' },
  { label: 'card1 (DRI secondary)',            path: '/dev/dri/card1',      gid: '44',  uid: '0' },
  { label: 'net/tun (Tailscale)',              path: '/dev/net/tun',        gid: '',    uid: '' },
  { label: 'fuse',                             path: '/dev/fuse',           gid: '',    uid: '' },
  { label: 'kvm',                              path: '/dev/kvm',            gid: '',    uid: '' },
];

const IDMAP_PRESETS = {
  full_u: { type: 'u', ct_id: 0, host_id: 100000, count: 65536 },
  full_g: { type: 'g', ct_id: 0, host_id: 100000, count: 65536 },
  root_u: { type: 'u', ct_id: 0, host_id: 0,      count: 1 },
  root_g: { type: 'g', ct_id: 0, host_id: 0,      count: 1 },
};

export function lxcAdvanced(node, vmid) {
  return {
    node, vmid,
    loading: true,
    errorMsg: '',
    LXC_FLAGS,

    features: {},
    desiredFeatures: {},
    devices: [],
    legacyDevices: [],
    mounts: [],
    idmaps: [],
    profiles: [],

    isRunning: false,
    canWrite: true,
    supportsDevN: true,
    pveVersion: '',
    hasBackup: false,
    backupTimestamp: '',

    flash: { show: false, cls: '', msg: '' },

    dev: { preset: '', path: '', gid: '', uid: '0' },
    idmap: { preset: '', type: 'u', ct_id: 0, host_id: 100000, count: 65536 },
    mount: { host_path: '', mp: '', ro: false, backup: false },

    previewDiffText: '',
    previewRunningWarn: false,
    pendingFeatures: {},

    profileDiff: null,
    profileTitle: '',
    profileApplyId: null,

    busy: { addDevice: false, addIdmap: false, addMount: false, applyFeatures: false, applyProfile: false },
    _modals: {},

    get idmapPreviewText() {
      return `lxc.idmap = ${this.idmap.type} ${this.idmap.ct_id} ${this.idmap.host_id} ${this.idmap.count}`;
    },

    get profileApplyDisabled() {
      const d = this.profileDiff;
      if (!d || d.loading || d.error) return true;
      if (d.alreadyApplied) return true;
      if (d.running && d.devicesRequireStop) return true;
      if (!d.supportsDevN && d.devicesToAdd && d.devicesToAdd.length > 0) return true;
      return false;
    },

    init() {
      ['addDeviceModal', 'addIdmapModal', 'addMountModal', 'previewModal', 'profileDiffModal']
        .forEach(r => { this._modals[r] = new bootstrap.Modal(this.$refs[r]); });
      this.load();
    },

    destroy() {
      Object.values(this._modals).forEach(m => m.dispose());
    },

    openModal(name) { this._modals[name + 'Modal']?.show(); },
    closeModal(name) { this._modals[name + 'Modal']?.hide(); },

    showFlash(cls, msg) {
      this.flash = { show: true, cls, msg };
      setTimeout(() => { this.flash.show = false; }, 5000);
    },

    async load() {
      this.loading = true;
      this.errorMsg = '';
      try {
        const [fData, dData, mData, iData] = await Promise.all([
          window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/features`),
          window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/devices`),
          window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/mounts`),
          window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/idmap`),
        ]);

        if (fData.error && dData.error && mData.error && iData.error) {
          this.errorMsg = fData.error;
          return;
        }

        this.features = fData.features || {};
        const df = {};
        LXC_FLAGS.forEach(f => { df[f.key] = (this.features[f.key] === '1'); });
        this.desiredFeatures = df;

        this.isRunning = fData.running || dData.running || mData.running || iData.running;
        this.canWrite = (fData.can_write !== false) && (dData.can_write !== false);
        this.supportsDevN = dData.pve_supports_dev_n !== false;
        this.pveVersion = dData.pve_version || '';
        this.hasBackup = !!fData.has_backup;
        this.backupTimestamp = fData.backup_timestamp || '';

        this.devices = dData.devices || [];
        this.legacyDevices = dData.legacy_devices || [];
        this.mounts = mData.mounts || [];
        this.idmaps = iData.idmaps || [];
      } catch {
        this.errorMsg = 'Failed to load advanced config';
      } finally {
        this.loading = false;
      }
      await this.loadProfiles();
    },

    refresh() { this.load(); },

    async loadProfiles() {
      try {
        const data = await window.ProxUtils.apiJson('/api/lxc-profiles');
        this.profiles = data.profiles || [];
      } catch {
        this.profiles = [];
      }
    },

    idmapDesc(m) {
      const end = m.ct_id + m.count - 1;
      const hEnd = m.host_id + m.count - 1;
      return `${m.ct_id}–${end} → ${m.host_id}–${hEnd} (${m.count})`;
    },

    // ── Feature flags ──────────────────────────────────────────────────────────

    previewFeatures() {
      const desired = {};
      LXC_FLAGS.forEach(f => { desired[f.key] = this.desiredFeatures[f.key] ? '1' : '0'; });
      const before = Object.entries(this.features).filter(([, v]) => v === '1').map(([k]) => `${k}=1`).join(',') || '(none)';
      const after = Object.entries(desired).filter(([, v]) => v === '1').map(([k]) => `${k}=1`).join(',') || '(none)';
      this.previewDiffText = `features: ${before}\n       → ${after}`;
      this.previewRunningWarn = this.isRunning;
      this.pendingFeatures = desired;
      this.openModal('preview');
    },

    async applyFeatures() {
      this.busy.applyFeatures = true;
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/features`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ features: this.pendingFeatures }),
        });
        this.closeModal('preview');
        this.showFlash(data.success ? 'success' : 'danger', data.success ? data.message : data.error);
        if (data.success) this.load();
      } catch {
        this.closeModal('preview');
        this.showFlash('danger', 'Network error');
      } finally {
        this.busy.applyFeatures = false;
      }
    },

    // ── Devices ────────────────────────────────────────────────────────────────

    openAddDevice() {
      this.dev = { preset: '', path: '', gid: '', uid: '0' };
      this.openModal('addDevice');
    },

    applyDevicePreset() {
      const opt = QUICKSYNC_DEVICES[this.dev.preset];
      if (!opt) return;
      this.dev.path = opt.path;
      this.dev.gid = opt.gid;
      this.dev.uid = opt.uid;
    },

    async addDevice() {
      if (!this.dev.path.trim()) { alert('Device path is required'); return; }
      this.busy.addDevice = true;
      try {
        const body = { path: this.dev.path.trim() };
        if (this.dev.gid) body.gid = this.dev.gid;
        if (this.dev.uid) body.uid = this.dev.uid;
        const data = await window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/devices`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        this.closeModal('addDevice');
        this.showFlash(data.success ? 'success' : 'danger', data.success ? data.message : data.error);
        if (data.success) this.load();
      } catch {
        this.showFlash('danger', 'Network error');
      } finally {
        this.busy.addDevice = false;
      }
    },

    async removeDevice(key) {
      if (!confirm(`Remove device entry ${key}?`)) return;
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/devices/${key}`, { method: 'DELETE' });
        this.showFlash(data.success ? 'success' : 'danger', data.success ? data.message : data.error);
        if (data.success) this.load();
      } catch {
        this.showFlash('danger', 'Network error');
      }
    },

    // ── Mounts ─────────────────────────────────────────────────────────────────

    openAddMount() {
      this.mount = { host_path: '', mp: '', ro: false, backup: false };
      this.openModal('addMount');
    },

    async addMount() {
      if (!this.mount.host_path.trim() || !this.mount.mp.trim()) {
        alert('Host path and container path are required'); return;
      }
      this.busy.addMount = true;
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/mounts`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            host_path: this.mount.host_path.trim(),
            mp: this.mount.mp.trim(),
            ro: this.mount.ro,
            backup: this.mount.backup,
          }),
        });
        this.closeModal('addMount');
        this.showFlash(data.success ? 'success' : 'danger', data.success ? data.message : data.error);
        if (data.success) this.load();
      } catch {
        this.showFlash('danger', 'Network error');
      } finally {
        this.busy.addMount = false;
      }
    },

    async removeMount(key) {
      if (!confirm(`Remove mount ${key}?`)) return;
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/mounts/${key}`, { method: 'DELETE' });
        this.showFlash(data.success ? 'success' : 'danger', data.success ? data.message : data.error);
        if (data.success) this.load();
      } catch {
        this.showFlash('danger', 'Network error');
      }
    },

    // ── UID/GID idmap ──────────────────────────────────────────────────────────

    openAddIdmap() {
      this.idmap = { preset: '', type: 'u', ct_id: 0, host_id: 100000, count: 65536 };
      this.openModal('addIdmap');
    },

    applyIdmapPreset() {
      const p = IDMAP_PRESETS[this.idmap.preset];
      if (!p) return;
      this.idmap.type = p.type;
      this.idmap.ct_id = p.ct_id;
      this.idmap.host_id = p.host_id;
      this.idmap.count = p.count;
    },

    async addIdmap() {
      const { type, ct_id, host_id, count } = this.idmap;
      if (!type || isNaN(ct_id) || isNaN(host_id) || isNaN(count)) {
        alert('All fields are required'); return;
      }
      this.busy.addIdmap = true;
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/idmap`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type, ct_id: +ct_id, host_id: +host_id, count: +count }),
        });
        this.closeModal('addIdmap');
        this.showFlash(data.success ? 'success' : 'danger', data.success ? data.message : data.error);
        if (data.success) this.load();
      } catch {
        this.showFlash('danger', 'Network error');
      } finally {
        this.busy.addIdmap = false;
      }
    },

    async removeIdmap(key) {
      if (!confirm(`Remove idmap entry ${key}?`)) return;
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/idmap/${key}`, { method: 'DELETE' });
        this.showFlash(data.success ? 'success' : 'danger', data.success ? data.message : data.error);
        if (data.success) this.load();
      } catch {
        this.showFlash('danger', 'Network error');
      }
    },

    // ── Restore ────────────────────────────────────────────────────────────────

    async restore() {
      if (!confirm('Restore the pre-write config snapshot? This will revert all Advanced/Passthrough changes.')) return;
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/config-restore`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
        });
        this.showFlash(data.success ? 'success' : 'danger', data.success ? data.message : data.error);
        if (data.success) this.load();
      } catch {
        this.showFlash('danger', 'Network error');
      }
    },

    // ── Profiles ───────────────────────────────────────────────────────────────

    async openPreviewProfile(profileId, profileName) {
      this.profileApplyId = profileId;
      this.profileTitle = profileName + ' — Preview';
      this.profileDiff = {
        loading: true, error: '', alreadyApplied: false,
        featuresToAdd: [], devicesToAdd: [],
        featuresAlreadySet: [], devicesAlreadyPresent: [],
        running: false, devicesRequireStop: false, supportsDevN: true,
      };
      this.openModal('profileDiff');
      try {
        const diff = await window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/profile-diff/${profileId}`);
        if (diff.error) {
          this.profileDiff = { ...this.profileDiff, loading: false, error: diff.error };
          return;
        }
        this.profileDiff = {
          loading: false,
          error: '',
          alreadyApplied: !diff.changes_needed,
          featuresToAdd: diff.features_to_add || [],
          devicesToAdd: diff.devices_to_add || [],
          featuresAlreadySet: diff.features_already_set || [],
          devicesAlreadyPresent: diff.devices_already_present || [],
          running: !!diff.running,
          devicesRequireStop: !!diff.devices_require_stop,
          supportsDevN: diff.pve_supports_dev_n !== false,
        };
      } catch {
        this.profileDiff = { ...this.profileDiff, loading: false, error: 'Failed to load diff' };
      }
    },

    async applyProfile() {
      this.busy.applyProfile = true;
      try {
        const data = await window.ProxUtils.apiJson(`/api/vm/${node}/${vmid}/lxc/profile-apply/${this.profileApplyId}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}),
        });
        this.closeModal('profileDiff');
        this.showFlash(data.success ? 'success' : 'danger', data.success ? data.message : data.error);
        if (data.success) this.load();
      } catch {
        this.showFlash('danger', 'Network error');
      } finally {
        this.busy.applyProfile = false;
      }
    },
  };
}
