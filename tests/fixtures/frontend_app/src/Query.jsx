import axios from "axios";
import { useQuery } from "@tanstack/react-query";

export function Query() {
  // near-miss: no .catch, but React Query owns error handling ->
  // no_error_handling must NOT fire
  const items = useQuery({
    queryKey: ["items"],
    queryFn: () => axios.get("/items").then((r) => r.data),
  });

  // planted: identical shape outside a hook -> still flags
  const bare = () => fetch("/query-miss");

  return null;
}
