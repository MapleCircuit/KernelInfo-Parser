/**
 * db.js - IndexedDB wrapper for offline storage and persistence.
 * Manages cached files, symbols, kconfigs, tab sessions, and nodemap diagrams.
 */
const DB_NAME = "KernelInfoIDE_v2";
// Stores of cached API data (meta, tabs_state and nodemap_projects hold user state)
const DATA_STORES = ["files", "symbols", "kconfigs", "maintainers", "commits", "tools", "mail"];
// 5: data stores get an `updated_at` index, so entries can be pruned by age (pruneOlderThan)
const DB_VERSION = 5;
// How long an open blocked by another window's older connection may wait before the app runs uncached
const BLOCKED_OPEN_TIMEOUT_MS = 3000;

const REQUIRED_STORES = [
  { name: "meta", keyPath: "key" },
  { name: "files", keyPath: "key" },
  { name: "symbols", keyPath: "key" },
  { name: "kconfigs", keyPath: "key" },
  { name: "maintainers", keyPath: "key" },
  { name: "commits", keyPath: "key" },
  { name: "tools", keyPath: "key" },
  { name: "mail", keyPath: "key" },
  { name: "tabs_state", keyPath: "key" },
  { name: "nodemap_projects", keyPath: "id" }
];

class LocalDatabase {
  constructor() {
    this.db = null;
    this.ready = this.init();
  }

  async init() {
    if (!window.indexedDB) {
      console.warn("[LocalDB] IndexedDB not available in this browser environment.");
      return null;
    }

    return new Promise((resolve) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      let settled = false;
      const settle = (db) => {
        if (settled) return;
        settled = true;
        clearTimeout(blockedTimer);
        resolve(db);
      };
      let blockedTimer = null;

      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        const tx = e.target.transaction;
        REQUIRED_STORES.forEach((s) => {
          const store = db.objectStoreNames.contains(s.name)
            ? tx.objectStore(s.name)
            : db.createObjectStore(s.name, { keyPath: s.keyPath });
          if (DATA_STORES.includes(s.name) && !store.indexNames.contains("updated_at")) {
            store.createIndex("updated_at", "updated_at");
          }
        });
      };

      req.onsuccess = (e) => {
        const db = e.target.result;
        // A newer page (new DB_VERSION) wants to upgrade: let go at once, or its open blocks until
        // every window holding this connection is closed
        db.onversionchange = () => {
          db.close();
          if (this.db === db) this.db = null;
        };
        if (settled) {
          // Opened after the blocked fallback below: from now on the cache is usable
          this.db = db;
          return;
        }
        this.db = db;
        settle(db);
      };

      // Another window still holds an older version open: run without cache rather than wait for it
      req.onblocked = () => {
        console.warn("[LocalDB] Upgrade blocked by another open window; continuing without the offline cache for now.");
        blockedTimer = setTimeout(() => settle(null), BLOCKED_OPEN_TIMEOUT_MS);
      };

      req.onerror = (e) => {
        console.warn("[LocalDB] IndexedDB open failed, falling back to memory/local storage:", e);
        settle(null);
      };
    });
  }

  async get(storeName, key) {
    await this.ready;
    if (!this.db || !this.db.objectStoreNames.contains(storeName)) {
      return null;
    }

    return new Promise((resolve) => {
      try {
        const tx = this.db.transaction(storeName, "readonly");
        const store = tx.objectStore(storeName);
        const req = store.get(key);
        req.onsuccess = () => resolve(req.result ? req.result.value : null);
        req.onerror = () => resolve(null);
      } catch (e) {
        console.warn(`[LocalDB] Get failed for ${storeName}/${key}:`, e);
        resolve(null);
      }
    });
  }

  async set(storeName, key, value) {
    await this.ready;
    if (!this.db || !this.db.objectStoreNames.contains(storeName)) {
      return false;
    }

    return new Promise((resolve) => {
      try {
        const tx = this.db.transaction(storeName, "readwrite");
        const store = tx.objectStore(storeName);
        const req = store.put({ key, value, updated_at: Date.now() });
        req.onsuccess = () => resolve(true);
        req.onerror = () => resolve(false);
      } catch (e) {
        console.warn(`[LocalDB] Set failed for ${storeName}/${key}:`, e);
        resolve(false);
      }
    });
  }

  async delete(storeName, key) {
    await this.ready;
    if (!this.db || !this.db.objectStoreNames.contains(storeName)) {
      return false;
    }

    return new Promise((resolve) => {
      try {
        const tx = this.db.transaction(storeName, "readwrite");
        const store = tx.objectStore(storeName);
        const req = store.delete(key);
        req.onsuccess = () => resolve(true);
        req.onerror = () => resolve(false);
      } catch (e) {
        console.warn(`[LocalDB] Delete failed for ${storeName}/${key}:`, e);
        resolve(false);
      }
    });
  }

  async clearStore(storeName) {
    await this.ready;
    if (!this.db || !this.db.objectStoreNames.contains(storeName)) {
      return false;
    }

    return new Promise((resolve) => {
      try {
        const tx = this.db.transaction(storeName, "readwrite");
        const store = tx.objectStore(storeName);
        const req = store.clear();
        req.onsuccess = () => resolve(true);
        req.onerror = () => resolve(false);
      } catch (e) {
        console.warn(`[LocalDB] Clear failed for ${storeName}:`, e);
        resolve(false);
      }
    });
  }

  async clearAllStores() {
    await this.ready;
    if (!this.db) return false;
    const storeNames = Array.from(this.db.objectStoreNames);
    await Promise.all(storeNames.map((s) => this.clearStore(s)));
    return true;
  }

  async clearDataStores() {
    await this.ready;
    await Promise.all(DATA_STORES.map((s) => this.clearStore(s)));
    return true;
  }

  /** Delete the cached API entries whose key matches `predicate` (one key scan per data store). Returns the count. */
  async deleteDataWhere(predicate) {
    await this.ready;
    if (!this.db) return 0;
    let count = 0;
    await Promise.all(DATA_STORES.filter((s) => this.db.objectStoreNames.contains(s)).map((s) => new Promise((resolve) => {
      try {
        const tx = this.db.transaction(s, "readwrite");
        const store = tx.objectStore(s);
        const req = store.openKeyCursor();
        req.onsuccess = () => {
          const cursor = req.result;
          if (!cursor) return;
          if (predicate(String(cursor.key))) {
            store.delete(cursor.primaryKey);
            count++;
          }
          cursor.continue();
        };
        tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
      } catch (e) {
        console.warn(`[LocalDB] Key scan failed for ${s}:`, e);
        resolve();
      }
    })));
    return count;
  }

  /**
   * Delete the cached API entries written more than `maxAgeMs` ago (a key-only scan of each data
   * store's `updated_at` index). Nothing else bounds the offline cache. Returns the count.
   */
  async pruneOlderThan(maxAgeMs) {
    await this.ready;
    if (!this.db) return 0;
    const cutoff = Date.now() - maxAgeMs;
    let count = 0;
    await Promise.all(DATA_STORES.filter((s) => this.db.objectStoreNames.contains(s)).map((s) => new Promise((resolve) => {
      try {
        const tx = this.db.transaction(s, "readwrite");
        const store = tx.objectStore(s);
        if (!store.indexNames.contains("updated_at")) {
          resolve();
          return;
        }
        const req = store.index("updated_at").openKeyCursor(IDBKeyRange.upperBound(cutoff));
        req.onsuccess = () => {
          const cursor = req.result;
          if (!cursor) return;
          store.delete(cursor.primaryKey);
          count++;
          cursor.continue();
        };
        tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
      } catch (e) {
        console.warn(`[LocalDB] Prune failed for ${s}:`, e);
        resolve();
      }
    })));
    return count;
  }

  async getStorageEstimate() {
    if (typeof navigator !== "undefined" && navigator.storage && navigator.storage.estimate) {
      try {
        const est = await navigator.storage.estimate();
        return {
          usage: est.usage || 0,
          quota: est.quota || 0
        };
      } catch {
        return null;
      }
    }
    return null;
  }

  async getStoreCounts() {
    await this.ready;
    if (!this.db) return {};
    const counts = {};
    const storeNames = Array.from(this.db.objectStoreNames);
    await Promise.all(
      storeNames.map((name) => {
        return new Promise((resolve) => {
          try {
            const tx = this.db.transaction(name, "readonly");
            const store = tx.objectStore(name);
            const req = store.count();
            req.onsuccess = () => {
              counts[name] = req.result;
              resolve();
            };
            req.onerror = () => {
              counts[name] = 0;
              resolve();
            };
          } catch {
            counts[name] = 0;
            resolve();
          }
        });
      })
    );
    return counts;
  }
}

export const localDB = new LocalDatabase();
