import axios from "axios";

export function OrderForm() {
  const submit = async (data) => {
    // no catch, no timeout -> flags; matches POST /api/orders
    const res = await axios.post("/api/orders", data);
    return res.data;
  };
  const loadOrder = (id) => axios.get(`/api/orders/${id}`).catch(() => null);
  return <button onClick={() => submit({ id: 1 })}>Order</button>;
}
