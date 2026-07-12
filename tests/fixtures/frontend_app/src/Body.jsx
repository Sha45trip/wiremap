import axios from "axios";

export function Body() {
  // planted: sends `extra` (not in ItemCreate) -> request_contract_mismatch
  const create = () =>
    axios.post("/contract/create", { name: "x", price: 1, extra: true })
      .catch(() => null);

  // planted: omits required `price` -> missing_request_field
  const partial = () =>
    axios.put("/contract/update/5", { name: "x" }).catch(() => null);

  // near-miss: spread -> incomplete body, neither check fires
  const spread = (data) =>
    axios.post("/contract/create", { name: "x", ...data }).catch(() => null);

  // near-miss: sends exactly required + optional -> silent
  const ok = () =>
    axios.post("/contract/create", { name: "x", price: 1, note: "n" })
      .catch(() => null);

  // fetch body via JSON.stringify, planted extra field
  const viaFetch = () =>
    fetch("/contract/create", {
      method: "POST",
      body: JSON.stringify({ name: "x", price: 1, ghost: 9 }),
    }).catch(() => null);

  return null;
}
