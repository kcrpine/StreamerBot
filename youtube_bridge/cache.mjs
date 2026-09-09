export class ExpiringLruCache {
  constructor({ maxEntries, now = Date.now }) {
    if (!Number.isInteger(maxEntries) || maxEntries < 1) {
      throw new TypeError('maxEntries must be a positive integer');
    }
    this.maxEntries = maxEntries;
    this.now = now;
    this.entries = new Map();
    this.pending = new Map();
  }

  get size() {
    return this.entries.size;
  }

  get(key) {
    const entry = this.entries.get(key);
    if (!entry) return undefined;
    if (entry.expiresAt <= this.now()) {
      this.entries.delete(key);
      return undefined;
    }
    this.entries.delete(key);
    this.entries.set(key, entry);
    return entry.value;
  }

  set(key, value, ttlMs) {
    if (!Number.isFinite(ttlMs) || ttlMs <= 0) return value;
    this.entries.delete(key);
    this.entries.set(key, { value, expiresAt: this.now() + ttlMs });
    while (this.entries.size > this.maxEntries) {
      this.entries.delete(this.entries.keys().next().value);
    }
    return value;
  }

  delete(key) {
    this.entries.delete(key);
    this.pending.delete(key);
  }

  dump() {
    const data = [];
    const now = this.now();
    for (const [key, entry] of this.entries) {
      if (entry && entry.expiresAt > now) {
        data.push([key, entry.value, entry.expiresAt]);
      }
    }
    return data;
  }

  load(data) {
    if (!Array.isArray(data)) return;
    const now = this.now();
    for (const [key, value, expiresAt] of data) {
      if (key && value && expiresAt > now) {
        this.entries.set(key, { value, expiresAt });
      }
    }
    while (this.entries.size > this.maxEntries) {
      this.entries.delete(this.entries.keys().next().value);
    }
  }

  async getOrCreate(key, ttlMs, create) {
    const cached = this.get(key);
    if (cached !== undefined) return cached;
    const pending = this.pending.get(key);
    if (pending) return pending;

    const work = Promise.resolve()
      .then(create)
      .then((value) => this.set(key, value, ttlMs))
      .finally(() => this.pending.delete(key));
    this.pending.set(key, work);
    return work;
  }
}
