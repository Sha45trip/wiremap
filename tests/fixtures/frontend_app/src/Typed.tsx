import axios from "axios";
import { SomeLibType } from "some-lib";

interface ItemView {
  id: number;
  name: string;
  price: number;
  discount?: number;
}

type GhostView = {
  id: number;
  ghost_total: number;
};

export function Typed() {
  // declared type matches ItemOut exactly; optional `discount` missing
  // backend-side must NOT flag (near-miss)
  const one = () => axios.get<ItemView>("/contract/item").catch(() => null);

  // planted: required ghost_total is not declared by ItemOut
  const two = () => axios.get<GhostView>("/contract/item2").catch(() => null);

  // near-miss: imported type we can't resolve -> no typed contract
  const three = () => axios.get<SomeLibType>("/contract/raw").catch(() => null);

  return null;
}
