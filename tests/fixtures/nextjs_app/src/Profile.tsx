import { trpc } from "../utils/trpc";

export function Profile() {
  // wires to QUERY /trpc#user.byId
  const user = trpc.user.byId.useQuery("1");
  // wires to MUTATION /trpc#user.update
  const update = trpc.user.update.useMutation();
  // planted: procedure the router does not define -> orphan_call
  const ghost = trpc.user.phantom.useQuery();
  return null;
}
