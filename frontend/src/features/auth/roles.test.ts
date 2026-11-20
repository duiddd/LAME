/**
 * | Test | Purpose | Method |
 * | --- | --- | --- |
 * | only ADMIN sees the dashboard | The nav link and route follow the role | ADMIN -> true; USER, signed out, missing role -> false |
 */
import { describe, expect, it } from "vitest";
import type { User } from "../../shared/types";
import { isAdmin } from "./roles";

describe("isAdmin", () => {
  it("only ADMIN sees the dashboard", () => {
    // Purpose: a plain user, a signed-out visitor and an old /me answer without a role all stay out.
    expect(isAdmin({ user_id: 1000, username: "admin", role: "ADMIN" })).toBe(true);
    expect(isAdmin({ user_id: 100000, username: "amy", role: "USER" })).toBe(false);
    expect(isAdmin(null)).toBe(false);
    expect(isAdmin({ user_id: 100000, username: "amy" } as unknown as User)).toBe(false);
  });
});
