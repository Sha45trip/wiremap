import { initTRPC } from "@trpc/server";

const t = initTRPC.create();
const publicProcedure = t.procedure;

const userRouter = t.router({
  byId: publicProcedure.input(z.string()).query(({ input }) => ({ id: input })),
  update: publicProcedure.mutation(() => ({ ok: true })),
});

export const appRouter = t.router({
  user: userRouter,
  health: publicProcedure.query(() => "ok"),
});
