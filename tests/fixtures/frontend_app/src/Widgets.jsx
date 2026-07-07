import axios from "axios";

export function Widgets() {
  // planted: literal fetch, no catch, no timeout -> no_error_handling + no_timeout
  const plainGet = () => fetch("/widgets");

  // method inferred from fetch options object
  const createItem = () => fetch("/items", { method: "POST", body: "{}" });

  // template literal -> /items/:param, PROBABLE confidence
  const getOne = (id) => axios.get(`/items/${id}`).catch(() => null);

  // string concatenation -> /items/:param, PROBABLE confidence
  const removeOne = (id) => axios.delete("/items/" + id).catch(() => null);

  // fully dynamic URL -> <dynamic>, INFERRED, unresolvable_url at match time
  const anyUrl = (u) => fetch(u).catch(() => null);

  return null;
}
