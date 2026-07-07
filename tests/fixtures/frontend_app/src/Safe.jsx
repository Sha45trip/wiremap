export function Safe() {
  // near-miss: .catch is several links down the promise chain -> must NOT
  // flag no_error_handling (regression guard for the Phase 1 fluent-chain bug)
  const chained = () =>
    fetch("/items")
      .then((r) => r.json())
      .then((d) => d)
      .catch(() => null);

  // near-miss: try/catch + AbortController signal -> neither flag fires
  async function guarded() {
    try {
      const ctrl = new AbortController();
      const r = await fetch("/api/v2/health", { signal: ctrl.signal });
      return await r.json();
    } catch {
      return null;
    }
  }

  return null;
}
