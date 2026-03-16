import { getAllSecrets, getSecret } from './infisicalClient.js';

const REFRESH_INTERVAL_MS = 5 * 60 * 1000;

class RuntimeVault {
  constructor() {
    this.memory = new Map();
    this.initialized = false;
    this.refreshTimer = null;
    this.initPromise = null;
    this.exitHooksRegistered = false;
  }

  async initialize() {
    if (this.initialized) return;
    if (this.initPromise) return this.initPromise;

    this.initPromise = (async () => {
      try {
        const secrets = await getAllSecrets({ forceRefresh: true });
        Object.entries(secrets).forEach(([key, value]) => this.memory.set(key, value));
        this.startRefresh();
      } catch {
        // Keep vault operational without leaking details in logs.
      }
      this.initialized = true;
      this.registerExitHooks();
    })();

    try {
      await this.initPromise;
    } finally {
      this.initPromise = null;
    }
  }

  startRefresh() {
    if (this.refreshTimer) clearInterval(this.refreshTimer);
    this.refreshTimer = setInterval(async () => {
      try {
        const secrets = await getAllSecrets({ forceRefresh: true });
        this.memory.clear();
        Object.entries(secrets).forEach(([key, value]) => this.memory.set(key, value));
      } catch {
        // silent by design to avoid secret leakage and noisy logs
      }
    }, REFRESH_INTERVAL_MS);
    this.refreshTimer.unref?.();
  }

  async get(key) {
    if (!this.initialized) {
      await this.initialize();
    }

    if (this.memory.has(key)) {
      return this.memory.get(key);
    }

    try {
      const value = await getSecret(key);
      if (value !== null && value !== undefined) {
        this.memory.set(key, value);
      }
      return value;
    } catch {
      return null;
    }
  }

  clear() {
    for (const [key, value] of this.memory.entries()) {
      if (typeof value === 'string') {
        this.memory.set(key, '\0'.repeat(value.length));
      }
      this.memory.delete(key);
    }
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
    this.initialized = false;
  }

  registerExitHooks() {
    if (this.exitHooksRegistered) return;
    this.exitHooksRegistered = true;

    const wipe = () => {
      this.clear();
    };

    process.on('exit', wipe);
    process.on('SIGINT', () => {
      wipe();
      process.exit(0);
    });
    process.on('SIGTERM', () => {
      wipe();
      process.exit(0);
    });
  }
}

const vault = new RuntimeVault();

export default vault;
