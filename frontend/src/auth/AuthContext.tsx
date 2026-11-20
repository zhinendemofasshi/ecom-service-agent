import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, configureClient } from "@/api/client";
import type { Identity } from "@/api/types";

const STORAGE_KEY = "ecom-console-token";

// sessionStorage keeps the token per tab and drops it when the tab closes.
// Storage can throw (private mode, blocked site data), so every access is guarded.
const storage = {
  get: () => {
    try {
      return sessionStorage.getItem(STORAGE_KEY);
    } catch {
      return null;
    }
  },
  set: (value: string | null) => {
    try {
      if (value) sessionStorage.setItem(STORAGE_KEY, value);
      else sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      /* in-memory only */
    }
  },
};

interface AuthState {
  identity: Identity | null;
  status: "loading" | "anonymous" | "authenticated";
  signIn: (token: string) => Promise<Identity>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const tokenRef = useRef<string | null>(storage.get());
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [status, setStatus] = useState<AuthState["status"]>(tokenRef.current ? "loading" : "anonymous");

  const clear = useCallback(() => {
    tokenRef.current = null;
    storage.set(null);
    setIdentity(null);
    setStatus("anonymous");
  }, []);

  configureClient({ getToken: () => tokenRef.current, onUnauthorized: clear });

  useEffect(() => {
    if (!tokenRef.current) return;
    api.me().then(
      (me) => {
        setIdentity(me);
        setStatus("authenticated");
      },
      () => clear(),
    );
  }, [clear]);

  const signIn = useCallback(async (token: string) => {
    const me = await api.me(token); // validate before storing
    tokenRef.current = token;
    storage.set(token);
    setIdentity(me);
    setStatus("authenticated");
    return me;
  }, []);

  const signOut = useCallback(async () => {
    await api.logout().catch(() => undefined);
    clear();
  }, [clear]);

  const value = useMemo(() => ({ identity, status, signIn, signOut }), [identity, status, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
