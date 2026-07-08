import axios from "axios";

export function Contract() {
  // planted: reads missing_field, which ItemOut does not declare
  const awaited = async () => {
    const res = await axios.get("/contract/item");
    return { id: res.data.id, label: res.data.name, ghost: res.data.missing_field };
  };

  // planted: fetch then-chain reads phantom off the parsed json
  const chained = () =>
    fetch("/contract/item2")
      .then((r) => r.json())
      .then((d) => d.price + d.phantom)
      .catch(() => null);

  // near-miss: endpoint has no declared model -> reads must not flag
  const untyped = async () => {
    const r = await fetch("/contract/raw");
    const d = await r.json();
    return d.whatever;
  };

  // near-miss: List[ItemOut] endpoint -> no certain field set; .map is a
  // builtin and x.id is not tracked (array elements out of scope)
  const listing = () =>
    axios.get("/contract/items").then((r) => r.data.map((x) => x.id)).catch(() => null);

  // near-miss: destructures only declared fields -> no flag
  const inline = async () => {
    const { name, price } = (await axios.get("/contract/item2")).data;
    return name + price;
  };

  return null;
}
