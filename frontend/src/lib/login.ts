"use client";

// Who is logged in, kept in the browser's localStorage so a reload keeps you in.
// The token is a login for the ORM_AI backend only; no AI or database keys ever
// reach the browser. Read it in components with useLogin().

import { useSyncExternalStore } from "react";

import type { Role } from "@/types/api";

export type Login = {
  token: string;
  userId: string;
  name: string | null;
  role: Role;
  shopId: string | null;
  shopName: string | null;
  expiresAt: number; // milliseconds since 1970
};

const KEY = "orm_ai.login";
const listeners = new Set<() => void>();
let cached: Login | null | undefined;

function read(): Login | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const login = JSON.parse(raw) as Login;
    if (!login.token || !login.userId || login.expiresAt <= Date.now()) return null;
    return login;
  } catch {
    return null; // storage switched off, or an old format: start logged out
  }
}

function emit(): void {
  listeners.forEach((listener) => listener());
}

export function getLogin(): Login | null {
  if (cached === undefined) cached = read();
  if (cached && cached.expiresAt <= Date.now()) cached = null;
  return cached;
}

export function setLogin(login: Login | null): void {
  cached = login;
  try {
    if (login) window.localStorage.setItem(KEY, JSON.stringify(login));
    else window.localStorage.removeItem(KEY);
  } catch {
    // Private windows may refuse storage; the login still lasts until the tab closes.
  }
  emit();
}

function onStorage(event: StorageEvent): void {
  if (event.key !== KEY) return;
  cached = read(); // logged in or out in another tab
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (listeners.size === 1) window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) window.removeEventListener("storage", onStorage);
  };
}

/**
 * The current login: a Login, null when logged out, or undefined while the page is
 * still being prepared on the server (the browser has not been asked yet).
 */
export function useLogin(): Login | null | undefined {
  return useSyncExternalStore<Login | null | undefined>(subscribe, getLogin, () => undefined);
}

export const ROLE_LABEL: Record<Role, string> = { owner: "Owner", staff: "Staff", admin: "Admin" };
