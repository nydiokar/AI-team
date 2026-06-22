/** Canonical Session fixtures — produced via the real adapter from rawSessions. */
import { toSessions } from "../transport/sessionAdapter";
import { toTargets } from "../transport/nodeAdapter";
import { rawSessions, rawNodes } from "./rawFixtures";

export const sessionFixtures = toSessions(rawSessions);
export const targetFixtures = toTargets(rawNodes);
