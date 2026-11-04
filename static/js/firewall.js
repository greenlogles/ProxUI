// static/js/firewall.js — firewall card for the cluster, node and guest pages

import * as U from './utils.js';

const LOG_LEVELS = ['emerg', 'alert', 'crit', 'err', 'warning', 'notice', 'info', 'debug', 'nolog'];
const POLICY_LABELS = { policy_in: 'Input policy', policy_out: 'Output policy', policy_forward: 'Forward policy' };
const RULE_TEXT_FIELDS = ['macro', 'proto', 'source', 'dest', 'sport', 'dport', 'iface', 'icmp-type', 'comment', 'log'];

function emptyRule() {
  const r = { pos: null, type: 'in', action: 'ACCEPT', enable: true, digest: '' };
  for (const k of RULE_TEXT_FIELDS) r[k] = '';
  return r;
}

export function firewallPanel(scope, node, vmid, group = '') {
  const base = scope === 'cluster' ? '/api/cluster/firewall'
    : scope === 'group' ? `/api/cluster/firewall/groups/${encodeURIComponent(group)}`
    : scope === 'node' ? `/api/node/${node}/firewall`
    : `/api/vm/${node}/${vmid}/firewall`;
  const json = (method, body) => ({
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });

  return {
    scope, group,
    loading: true, loaded: false, busy: false, error: '',
    options: {}, defaults: {}, digest: '', clusterEnabled: null, nics: [],
    rules: [],
    macros: [], groups: [], addresses: [],
    logLevels: LOG_LEVELS,
    form: emptyRule(), formError: '',
    confirm: { title: '', body: '', warn: '', label: '', danger: false, run: null },
    _ruleModal: null, _confirmModal: null,

    get enabled() {
      const v = this.options.enable ?? this.defaults.enable;
      return !!Number(v);
    },
    get policyKeys() {
      return Object.keys(POLICY_LABELS).filter(k => k in this.defaults);
    },
    policyLabel(key) { return POLICY_LABELS[key] || key; },
    policyChoices(key) {
      return key === 'policy_forward' ? ['ACCEPT', 'DROP'] : ['ACCEPT', 'REJECT', 'DROP'];
    },
    portText(r) {
      if (r.macro) return '—';
      const parts = [];
      if (r.dport) parts.push(r.dport);
      if (r.sport) parts.push('from ' + r.sport);
      return parts.join(' ') || 'any';
    },

    async load() {
      this.loading = true; this.error = '';
      try {
        // Security groups have rules but no options of their own.
        const [opts, rules] = await Promise.all([
          scope === 'group' ? Promise.resolve({}) : U.apiJson(`${base}/options`),
          U.apiJson(`${base}/rules`),
        ]);
        this.options = opts.options || {};
        this.defaults = opts.defaults || {};
        this.digest = opts.digest || '';
        this.clusterEnabled = opts.cluster_enabled ?? null;
        this.nics = opts.nics || [];
        this.rules = rules.rules || [];
        this.loaded = true;
      } catch (e) {
        this.error = e.message;
      } finally {
        this.loading = false;
      }
    },

    // Reloaded on every dialog open so aliases / IP sets / groups created on
    // the other tabs show up without a page refresh.
    async loadChoices() {
      try {
        const c = await U.apiJson('/api/firewall/choices');
        this.macros = c.macros || [];
        this.groups = c.groups || [];
        this.addresses = c.addresses || [];
      } catch (e) {
        U.notify('Could not load firewall macros: ' + e.message, 'error');
      }
    },

    async _run(fn, okMsg) {
      this.busy = true;
      try {
        const r = await fn();
        U.notify(okMsg || r.message, 'success');
        return true;
      } catch (e) {
        U.notify('Firewall: ' + e.message, 'error');
        return false;
      } finally {
        this.busy = false;
        await this.load();
      }
    },

    _ask(c) {
      this.confirm = c;
      this._confirmModal = this._confirmModal || new bootstrap.Modal(this.$refs.confirmModal);
      this._confirmModal.show();
    },
    async runConfirm() {
      const run = this.confirm.run;
      this._confirmModal.hide();
      if (run) await run();
    },

    askToggle() {
      const turnOn = !this.enabled;
      const where = this.scope === 'cluster' ? 'the datacenter' : this.scope === 'node' ? `node ${node}` : `guest ${vmid}`;
      let warn = '';
      if (turnOn && this.scope === 'cluster') {
        // With the datacenter firewall on and the default DROP input policy,
        // only the cluster network keeps access to 8006/22 — ProxUI included.
        warn = 'With the default input policy (DROP), Proxmox only allows the web API (8006) and SSH from the ' +
          "cluster's local network. If ProxUI or your browser reaches the nodes from another network, " +
          'add an ACCEPT rule for it first or you will lose access.';
      } else if (turnOn && this.scope === 'guest' && (this.options.policy_in ?? this.defaults.policy_in) !== 'ACCEPT') {
        warn = 'Incoming traffic not matched by a rule will be dropped.';
      }
      this._ask({
        title: turnOn ? 'Enable firewall' : 'Disable firewall',
        body: `${turnOn ? 'Enable' : 'Disable'} the firewall for ${where}?`,
        warn,
        label: turnOn ? 'Enable' : 'Disable',
        danger: !turnOn,
        run: () => this._run(() => U.apiJson(`${base}/options`,
          json('PUT', { enable: turnOn, digest: this.digest })),
          turnOn ? 'Firewall enabled.' : 'Firewall disabled.'),
      });
    },

    savePolicy(key, value) {
      return this._run(() => U.apiJson(`${base}/options`, json('PUT', { [key]: value, digest: this.digest })));
    },

    async openRule(rule) {
      this.formError = '';
      if (rule) {
        const f = emptyRule();
        for (const k of Object.keys(f)) if (rule[k] !== undefined && rule[k] !== null) f[k] = rule[k];
        f.enable = !!Number(rule.enable);
        this.form = f;
      } else {
        this.form = emptyRule();
      }
      this._ruleModal = this._ruleModal || new bootstrap.Modal(this.$refs.ruleModal);
      this._ruleModal.show();
      await this.loadChoices();
    },

    async saveRule() {
      const f = { ...this.form };
      if (f.type === 'group' && !f.action) { this.formError = 'Choose a security group.'; return; }
      if (f.type !== 'group' && !['ACCEPT', 'DROP', 'REJECT'].includes(f.action)) f.action = 'ACCEPT';
      if (f.type === 'group') {
        for (const k of ['macro', 'proto', 'source', 'dest', 'sport', 'dport', 'icmp-type', 'log']) f[k] = '';
      } else if (f.macro) {
        // A macro supplies protocol and ports itself; PVE rejects both.
        f.proto = f.sport = f.dport = f['icmp-type'] = '';
      }
      const editing = f.pos !== null;
      const url = editing ? `${base}/rules/${f.pos}` : `${base}/rules`;
      const body = { ...f };
      delete body.pos;
      if (!editing) delete body.digest;
      this.formError = '';
      this.busy = true;
      try {
        const r = await U.apiJson(url, json(editing ? 'PUT' : 'POST', body));
        this._ruleModal.hide();
        U.notify(r.message, 'success');
      } catch (e) {
        this.formError = e.message;
      } finally {
        this.busy = false;
      }
      await this.load();
    },

    toggleRule(r) {
      return this._run(() => U.apiJson(`${base}/rules/${r.pos}`,
        json('PUT', { enable: !Number(r.enable), digest: r.digest })));
    },

    move(r, dir) {
      return this._run(() => U.apiJson(`${base}/rules/${r.pos}`, json('PUT', { move: dir, digest: r.digest })));
    },

    askDelete(r) {
      const what = r.macro || r.proto || 'any';
      this._ask({
        title: 'Delete firewall rule',
        body: `Delete rule #${r.pos} (${r.type} ${r.action} ${what})?`,
        warn: '',
        label: 'Delete',
        danger: true,
        run: () => this._run(() => U.apiJson(
          `${base}/rules/${r.pos}?digest=${encodeURIComponent(r.digest || '')}`, { method: 'DELETE' })),
      });
    },

    init() { this.load(); },
  };
}
