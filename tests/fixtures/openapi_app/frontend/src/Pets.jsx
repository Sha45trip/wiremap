export function Pets() {
  // generated-client calls: matched to the spec by operationId
  const list = () => api.listPets().catch(() => null);
  const one = (id) => api.getPetById(id).catch(() => null);
  // near-miss: no such operationId in the spec -> dropped, no node
  const nope = () => api.notARealOp().catch(() => null);
  // literal URL still matches the spec-ingested endpoint by path
  const plain = () => fetch("/pets").catch(() => null);
  return null;
}
