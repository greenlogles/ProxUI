// static/js/firewall-objects.js — security groups, aliases and IP sets on the Firewall page

import * as U from './utils.js';

const API = '/api/cluster/firewall';
const NAME_RE = /^[A-Za-z][A-Za-z0-9_-]+$/;

const json = (method, body) => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});
const enc = encodeURIComponent;

// Shared modal / confirm / busy plumbing for the three tabs.
function base() {
  return {
    loading: true, busy: false, error: '', formError: '',
    confirm: { title: '', body: '', run: null },
    _modals: {},

    modal(ref) {
      this._modals[ref] = this._modals[ref] || new bootstrap.Modal(this.$refs[ref]);
      return this._modals[ref];
    },
    ask(title, body, run) {
      this.confirm = { title, body, run };
      this.modal('confirmModal').show();
    },
    async runConfirm() {
      const run = this.confirm.run;
      this.modal('confirmModal').hide();
      if (run) await run();
    },
    // Runs a write, reports it, reloads. Returns true on success so callers
    // can close their dialog; on failure the error stays in the dialog.
    async write(fn, { inDialog = false } = {}) {
      this.busy = true; this.formError = '';
      try {
        const r = await fn();
        U.notify(r.message, 'success');
        return true;
      } catch (e) {
        if (inDialog) this.formError = e.message;
        else U.notify('Firewall: ' + e.message, 'error');
        return false;
      } finally {
        this.busy = false;
        await this.load();
      }
    },
    async fetchInto(url, key) {
      this.loading = true; this.error = '';
      try { this[key] = (await U.apiJson(url))[key] || []; }
      catch (e) { this.error = e.message; }
      finally { this.loading = false; }
    },
  };
}

export function fwGroups() {
  return {
    ...base(),
    groups: [], selected: '',
    form: { orig: '', group: '', comment: '' },

    async load() {
      await this.fetchInto(`${API}/groups`, 'groups');
      if (this.selected && !this.groups.some(g => g.group === this.selected)) this.selected = '';
    },
    open(g) {
      this.formError = '';
      this.form = g ? { orig: g.group, group: g.group, comment: g.comment || '' }
                    : { orig: '', group: '', comment: '' };
      this.modal('editModal').show();
    },
    async save() {
      const f = this.form;
      if (!NAME_RE.test(f.group) || f.group.length > 18) {
        this.formError = 'Name: 2-18 characters, letter first, then letters, digits, - or _';
        return;
      }
      const ok = await this.write(() => f.orig
        ? U.apiJson(`${API}/groups/${enc(f.orig)}`, json('PUT', { group: f.group, comment: f.comment }))
        : U.apiJson(`${API}/groups`, json('POST', { group: f.group, comment: f.comment })),
      { inDialog: true });
      if (ok) {
        this.modal('editModal').hide();
        if (this.selected === f.orig) this.selected = f.group;
      }
    },
    remove(g) {
      this.ask('Delete security group',
        `Delete security group '${g.group}'? Proxmox refuses while the group still has rules or is used by a rule.`,
        () => this.write(() => U.apiJson(`${API}/groups/${enc(g.group)}`, { method: 'DELETE' })));
    },
    init() { this.load(); },
  };
}

export function fwAliases() {
  return {
    ...base(),
    aliases: [],
    form: { orig: '', name: '', cidr: '', comment: '', digest: '' },

    load() { return this.fetchInto(`${API}/aliases`, 'aliases'); },
    open(a) {
      this.formError = '';
      this.form = a ? { orig: a.name, name: a.name, cidr: a.cidr, comment: a.comment || '', digest: a.digest }
                    : { orig: '', name: '', cidr: '', comment: '', digest: '' };
      this.modal('editModal').show();
    },
    async save() {
      const f = this.form;
      if (!NAME_RE.test(f.name)) { this.formError = 'Name: letter first, then letters, digits, - or _'; return; }
      if (!f.cidr.trim()) { this.formError = 'An IP address or CIDR is required'; return; }
      const body = { name: f.name, cidr: f.cidr.trim(), comment: f.comment };
      const ok = await this.write(() => f.orig
        ? U.apiJson(`${API}/aliases/${enc(f.orig)}`, json('PUT', { ...body, digest: f.digest }))
        : U.apiJson(`${API}/aliases`, json('POST', body)),
      { inDialog: true });
      if (ok) this.modal('editModal').hide();
    },
    remove(a) {
      this.ask('Delete alias', `Delete alias '${a.name}' (${a.cidr})? Proxmox refuses while a rule or IP set uses it.`,
        () => this.write(() => U.apiJson(`${API}/aliases/${enc(a.name)}?digest=${enc(a.digest || '')}`, { method: 'DELETE' })));
    },
    init() { this.load(); },
  };
}

export function fwIpsets() {
  return {
    ...base(),
    ipsets: [], selected: '',
    form: { orig: '', name: '', comment: '' },
    entry: { cidr: '', comment: '', nomatch: false },
    entryEdit: { cidr: '', comment: '', nomatch: false, digest: '' },

    get current() { return this.ipsets.find(s => s.name === this.selected) || null; },

    async load() {
      await this.fetchInto(`${API}/ipsets`, 'ipsets');
      if (this.selected && !this.current) this.selected = '';
    },
    open(s) {
      this.formError = '';
      this.form = s ? { orig: s.name, name: s.name, comment: s.comment || '' } : { orig: '', name: '', comment: '' };
      this.modal('editModal').show();
    },
    async save() {
      const f = this.form;
      if (!NAME_RE.test(f.name)) { this.formError = 'Name: letter first, then letters, digits, - or _'; return; }
      const ok = await this.write(() => f.orig
        ? U.apiJson(`${API}/ipsets/${enc(f.orig)}`, json('PUT', { name: f.name, comment: f.comment }))
        : U.apiJson(`${API}/ipsets`, json('POST', { name: f.name, comment: f.comment })),
      { inDialog: true });
      if (ok) {
        this.modal('editModal').hide();
        this.selected = f.name;
      }
    },
    remove(s) {
      const n = (s.entries || []).length;
      this.ask('Delete IP set',
        `Delete IP set '${s.name}'${n ? ` and its ${n} entr${n === 1 ? 'y' : 'ies'}` : ''}? Proxmox refuses while a rule uses it.`,
        () => this.write(() => U.apiJson(`${API}/ipsets/${enc(s.name)}`, { method: 'DELETE' })));
    },

    async addEntry() {
      const e = this.entry;
      if (!e.cidr.trim()) return;
      const ok = await this.write(() => U.apiJson(`${API}/ipsets/${enc(this.selected)}/entries`,
        json('POST', { cidr: e.cidr.trim(), comment: e.comment, nomatch: e.nomatch })));
      if (ok) this.entry = { cidr: '', comment: '', nomatch: false };
    },
    openEntry(e) {
      this.formError = '';
      this.entryEdit = { cidr: e.cidr, comment: e.comment || '', nomatch: !!Number(e.nomatch), digest: e.digest };
      this.modal('entryModal').show();
    },
    async saveEntry() {
      const e = this.entryEdit;
      // The CIDR goes into the path as-is (PVE expects the slash unencoded).
      const ok = await this.write(() => U.apiJson(`${API}/ipsets/${enc(this.selected)}/entries/${e.cidr}`,
        json('PUT', { comment: e.comment, nomatch: e.nomatch, digest: e.digest })), { inDialog: true });
      if (ok) this.modal('entryModal').hide();
    },
    removeEntry(e) {
      this.ask('Remove entry', `Remove ${e.cidr} from IP set '${this.selected}'?`,
        () => this.write(() => U.apiJson(
          `${API}/ipsets/${enc(this.selected)}/entries/${e.cidr}?digest=${enc(e.digest || '')}`, { method: 'DELETE' })));
    },
    init() { this.load(); },
  };
}
