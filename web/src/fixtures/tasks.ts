/** Canonical Task fixtures — produced via the real adapter from rawTasks. */
import { toTasks } from "../transport/taskAdapter";
import { rawTasks } from "./rawFixtures";

export const taskFixtures = toTasks(rawTasks);
