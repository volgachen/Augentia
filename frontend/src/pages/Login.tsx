import { useState } from "react";
import type { FormEvent } from "react";
import { useAuthStore } from "../store/auth";

export default function Login() {
  const login = useAuthStore((state) => state.login);
  const error = useAuthStore((state) => state.error);
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!password || submitting) return;
    setSubmitting(true);
    try {
      await login(password);
      setPassword("");
    } catch {
      // The store exposes a generic error message.
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="flex min-h-screen items-center justify-center bg-gray-950 px-4 text-gray-100">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm rounded-xl border border-gray-800 bg-gray-900 p-6 shadow-xl"
      >
        <h1 className="mb-1 text-xl font-semibold text-white">Augentia</h1>
        <p className="mb-6 text-sm text-gray-500">请输入访问密码</p>
        <label className="block text-xs font-medium text-gray-400" htmlFor="password">
          Password
        </label>
        <input
          id="password"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className="mt-1 w-full rounded-lg border border-gray-700 bg-gray-950 px-3 py-2 text-sm text-gray-100 outline-none focus:border-indigo-500"
        />
        {error && <p className="mt-2 text-xs text-red-300">{error}</p>}
        <button
          type="submit"
          disabled={!password || submitting}
          className="mt-5 w-full rounded-lg bg-indigo-600 px-3 py-2 text-sm font-medium text-white hover:bg-indigo-500 disabled:opacity-40"
        >
          {submitting ? "登录中…" : "登录"}
        </button>
      </form>
    </main>
  );
}
