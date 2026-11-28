// Module singletons (api.js -> state.js) start heartbeats on import: keep timers unreffed so node:test exits.
for (const k of ["setInterval", "setTimeout"]) {
  const real = globalThis[k];
  globalThis[k] = (...args) => {
    const t = real(...args);
    t?.unref?.();
    return t;
  };
}
