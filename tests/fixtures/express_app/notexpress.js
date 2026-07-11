// near-miss: no express import — client.get is a cache lookup, not a route
const client = createRedisClient();
client.get("/some/key", () => {});
client.post = () => {};
