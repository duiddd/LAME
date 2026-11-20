import type { User } from "../../shared/types";

/**
 * Whether the signed-in user may see the system dashboard. The role comes from A's /api/auth/me.
 * This only decides what the page shows; it does not protect the data behind the dashboard.
 */
export function isAdmin(user: User | null | undefined): boolean {
  return user?.role === "ADMIN";
}
