import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useStore } from "./store/sessions";
import { useAuthStore } from "./store/auth";
import { usePluginHostStore } from "./store/pluginHost";
import { BACKEND_ORIGIN } from "./api/backend";
import ActiveSessionsMenu from "./components/ActiveSessionsMenu";
import BackendSettings from "./components/BackendSettings";
import ToastCenter from "./components/ToastCenter";

const NAV = [
  { to: "/", label: "Agent Registry", end: true },
  { to: "/sessions", label: "Sessions" },
  { to: "/plugins", label: "Plugins" },
];

export default function App() {
  const sessions = useStore((s) => s.sessions);
  const authEnabled = useAuthStore((s) => s.authEnabled);
  const logout = useAuthStore((s) => s.logout);
  const setActiveSession = useStore((s) => s.setActiveSession);
  const sessionCount = Object.keys(sessions).length;
  // Running plugin services, as last seen by the plugin stream (live while a
  // page that uses it is open).
  const hostServices = usePluginHostStore((s) => s.services);
  const runningPlugins = Object.values(hostServices).filter((sv) => sv.status === "running").length;
  // Highlight the session we're viewing (if the current route is a console).
  const match = useLocation().pathname.match(/^\/console\/(.+)$/);
  const currentSessionId = match?.[1];
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    setActiveSession(currentSessionId ?? null);
  }, [currentSessionId, setActiveSession]);

  return (
    <div className="h-screen overflow-hidden bg-gray-950 text-gray-100 flex flex-col">
      {/* Top nav */}
      <nav className="shrink-0 border-b border-gray-800 px-6 py-3 flex items-center gap-6">
        <button
          type="button"
          onClick={() => setSettingsOpen(true)}
          title={`设置（后端：${BACKEND_ORIGIN}）`}
          className="font-semibold text-white tracking-tight hover:text-indigo-300 transition-colors"
        >
          Augentia
        </button>
        <div className="flex gap-1">
          {NAV.map(({ to, label, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `px-3 py-1.5 rounded-lg text-sm transition-colors ${
                  isActive
                    ? "bg-gray-800 text-white"
                    : "text-gray-400 hover:text-white hover:bg-gray-800/50"
                }`
              }
            >
              {label}
              {label === "Sessions" && sessionCount > 0 && (
                <span className="ml-1.5 text-xs bg-indigo-700 text-white px-1.5 py-0.5 rounded-full">
                  {sessionCount}
                </span>
              )}
              {label === "Plugins" && runningPlugins > 0 && (
                <span className="ml-1.5 text-xs bg-green-700 text-white px-1.5 py-0.5 rounded-full">
                  {runningPlugins}
                </span>
              )}
            </NavLink>
          ))}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <ActiveSessionsMenu currentSessionId={currentSessionId} />
          {authEnabled && (
            <button
              type="button"
              onClick={() => void logout()}
              className="rounded-md border border-gray-700 bg-gray-800 px-2.5 py-1 text-xs font-medium text-gray-300 hover:bg-gray-700"
            >
              Logout
            </button>
          )}
        </div>
      </nav>

      {/* Page content */}
      <main className="flex-1 flex flex-col min-h-0 overflow-hidden">
        <Outlet />
      </main>
      <ToastCenter />
      {settingsOpen && <BackendSettings onClose={() => setSettingsOpen(false)} />}
    </div>
  );
}
