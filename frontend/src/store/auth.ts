import { create } from "zustand";
import { api } from "../api/client";

type AuthStatus = "checking" | "authenticated" | "anonymous";

interface AuthStore {
  status: AuthStatus;
  authEnabled: boolean;
  error: string | null;
  check: () => Promise<void>;
  login: (password: string) => Promise<void>;
  logout: () => Promise<void>;
}

export const useAuthStore = create<AuthStore>((set) => ({
  status: "checking",
  authEnabled: true,
  error: null,

  check: async () => {
    try {
      const result = await api.auth.me();
      set({
        status: result.authenticated ? "authenticated" : "anonymous",
        authEnabled: result.auth_enabled,
        error: null,
      });
    } catch (error) {
      set({ status: "anonymous", error: (error as Error).message });
    }
  },

  login: async (password) => {
    set({ error: null });
    try {
      const result = await api.auth.login(password);
      set({
        status: result.authenticated ? "authenticated" : "anonymous",
        authEnabled: result.auth_enabled,
      });
    } catch (error) {
      set({ status: "anonymous", error: "密码错误或服务暂不可用。" });
      throw error;
    }
  },

  logout: async () => {
    await api.auth.logout();
    set({ status: "anonymous", error: null });
    window.location.reload();
  },
}));
