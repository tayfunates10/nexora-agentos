import "server-only";
import { cookies } from "next/headers";
import { cookieNames, loadSession, readConfig } from "../auth-core";
import { sessionStore } from "./redis";

export async function currentSession() {
  const config = readConfig();
  if (!config) return null;
  return loadSession(sessionStore, (await cookies()).get(cookieNames(config).session)?.value);
}
