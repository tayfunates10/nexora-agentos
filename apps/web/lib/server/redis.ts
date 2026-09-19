import "server-only";
import { createClient } from "redis";
import type { Store } from "../auth-core";

const makeClient = () => createClient({ url: process.env.NEXORA_SESSION_REDIS_URL,
  socket: { connectTimeout: 3000, reconnectStrategy: false }, disableOfflineQueue: true });
let connecting: Promise<ReturnType<typeof makeClient>> | undefined;
async function connection() {
  if (!connecting) {
    const client = makeClient();
    client.on("error", () => { connecting = undefined; });
    connecting = client.connect().then(() => client).catch(error => { connecting = undefined; throw error; });
  }
  return connecting!;
}
export const sessionStore: Store = {
  get: async key => (await connection()).withCommandOptions({ timeout: 3000 }).get(key),
  take: async key => (await connection()).withCommandOptions({ timeout: 3000 }).getDel(key),
  put: async (key, value, ttl) => { await (await connection()).withCommandOptions({ timeout: 3000 }).set(key, value, { EX: ttl }); },
  remove: async key => { await (await connection()).withCommandOptions({ timeout: 3000 }).del(key); },
};
