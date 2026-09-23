/**
 * db.js - IndexedDB wrapper for offline storage and persistence.
 * Manages cached files, symbols, kconfigs, tab sessions, and nodemap diagrams.
 */
const DB_NAME = "KernelInfoIDE_v2";
const DB_VERSION = 3;

const REQUIRED_STORES = [
  { name: "meta", keyPath: "key" },
  { name: "files", keyPath: "key" },
  { name: "symbols", keyPath: "key" },
  { name: "kconfigs", keyPath: "key" },
  { name: "maintainers", keyPath: "key" },
  { name: "commits", keyPath: "key" },
  { name: "tools", keyPath: "key" },
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

      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        REQUIRED_STORES.forEach((s) => {
          if (!db.objectStoreNames.contains(s.name)) {
            db.createObjectStore(s.name, { keyPath: s.keyPath });
          }
        });
      };

      req.onsuccess = (e) => {
        this.db = e.target.result;
        resolve(this.db);
      };

      req.onerror = (e) => {
        console.warn("[LocalDB] IndexedDB open failed, falling back to memory/local storage:", e);
        resolve(null);
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
    const dataStores = ["files", "symbols", "kconfigs", "maintainers", "commits", "tools"];
    await Promise.all(dataStores.map((s) => this.clearStore(s)));
    return true;
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
