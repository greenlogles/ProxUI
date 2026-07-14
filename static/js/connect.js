// static/js/connect.js — Alpine factory for cluster connection/management page
export function connectPage() {
  return {
    // Add-cluster form auth type
    authType: 'password',

    // Test connection state
    testing: false,
    testSuccess: null,
    testNode: '',
    testVersion: '',
    testWarning: '',
    testError: '',

    // Edit modal state
    editId: '',
    editName: '',
    editHost: '',
    editUser: '',
    editAuthType: 'password',
    editTokenName: '',
    editPassword: '',
    editTokenValue: '',
    editVerifySsl: true,
    editIncrementalVmid: false,
    saving: false,
    _editModal: null,

    init() {
      if (this.$refs.editModal) {
        this._editModal = new bootstrap.Modal(this.$refs.editModal);
      }
      // Auto-generate cluster ID from name
      // (handled via @input on the cluster_name field)
    },

    destroy() {
      this._editModal?.dispose();
    },

    generateId(name) {
      return name.toLowerCase()
        .replace(/[^a-z0-9\s]/g, '')
        .replace(/\s+/g, '_')
        .substring(0, 20);
    },

    async testConnection() {
      const form = document.getElementById('connectForm');
      const formData = new FormData(form);
      this.testing = true;
      this.testSuccess = null;
      this.testError = '';
      this.testWarning = '';
      try {
        const resp = await fetch('/api/test-connection', { method: 'POST', body: formData });
        const data = await resp.json();
        if (data.success) {
          this.testSuccess = true;
          this.testNode = data.node_name || '';
          this.testVersion = data.version || '';
          this.testWarning = data.warning || '';
        } else {
          this.testSuccess = false;
          this.testError = data.error || 'Connection failed';
        }
      } catch (e) {
        this.testSuccess = false;
        this.testError = e.message;
      } finally {
        this.testing = false;
      }
    },

    async switchCluster(id) {
      if (!confirm('Switch to this cluster? This will reload the page.')) return;
      try {
        const data = await window.ProxUtils.apiJson(`/api/switch-cluster/${id}`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
        });
        if (data.success) {
          localStorage.setItem('selectedClusterId', id);
          window.location.href = '/';
        } else {
          alert('Failed to switch cluster: ' + (data.error || 'Unknown error'));
        }
      } catch (e) { alert('Network error: ' + e.message); }
    },

    async openEdit(id) {
      try {
        const data = await window.ProxUtils.apiJson(`/api/cluster/${id}`);
        if (!data.success) { alert('Failed to load cluster: ' + (data.error || 'Unknown error')); return; }
        const cluster = data.cluster;
        const node = cluster.nodes[0] || {};
        this.editId = cluster.id;
        this.editName = cluster.name;
        this.editHost = node.host || '';
        this.editUser = node.user || '';
        this.editVerifySsl = node.verify_ssl !== false;
        this.editIncrementalVmid = cluster.incremental_vmid || false;
        this.editAuthType = node.auth_type === 'token' ? 'token' : 'password';
        this.editTokenName = node.token_name || '';
        this.editPassword = '';
        this.editTokenValue = '';
        this._editModal?.show();
      } catch (e) { alert('Network error: ' + e.message); }
    },

    async saveEdit() {
      const authType = this.editAuthType;
      const payload = {
        name: this.editName,
        incremental_vmid: this.editIncrementalVmid,
        nodes: [{
          host: this.editHost,
          user: this.editUser,
          verify_ssl: this.editVerifySsl,
          auth_type: authType,
        }],
      };
      if (authType === 'token') {
        if (this.editTokenName) payload.nodes[0].token_name = this.editTokenName;
        if (this.editTokenValue) payload.nodes[0].token_value = this.editTokenValue;
      } else {
        if (this.editPassword) payload.nodes[0].password = this.editPassword;
      }
      this.saving = true;
      try {
        const data = await window.ProxUtils.apiJson(`/api/cluster/${this.editId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (data.success) {
          this._editModal?.hide();
          window.location.reload();
        } else {
          alert('Failed to save cluster: ' + (data.error || 'Unknown error'));
        }
      } catch (e) { alert('Network error: ' + e.message); }
      finally { this.saving = false; }
    },

    async deleteCluster(id, name) {
      if (!confirm(`Are you sure you want to delete cluster "${name}"? This action cannot be undone.`)) return;
      try {
        const data = await window.ProxUtils.apiJson(`/api/delete-cluster/${id}`, { method: 'DELETE' });
        if (data.success) { alert('Cluster deleted successfully'); window.location.reload(); }
        else alert('Failed to delete cluster: ' + (data.error || 'Unknown error'));
      } catch (e) { alert('Network error: ' + e.message); }
    },
  };
}
