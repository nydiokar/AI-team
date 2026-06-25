import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn-style class combiner: clsx + tailwind-merge (dedupe conflicts). */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
