import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import "./index.css";
import App from "./App.tsx";
import AuthGuard from "./components/AuthGuard.tsx";
import AgentRegistry from "./pages/AgentRegistry.tsx";
import SessionDashboard from "./pages/SessionDashboard.tsx";
import LiveConsole from "./pages/LiveConsole.tsx";
import PluginRegistry from "./pages/PluginRegistry.tsx";
import PluginConsole from "./pages/PluginConsole.tsx";

const router = createBrowserRouter([
  {
    path: "/",
    element: (
      <AuthGuard>
        <App />
      </AuthGuard>
    ),
    children: [
      { index: true, element: <AgentRegistry /> },
      { path: "sessions", element: <SessionDashboard /> },
      { path: "console/:sessionId", element: <LiveConsole /> },
      { path: "plugins", element: <PluginRegistry /> },
      { path: "plugins/:pluginId", element: <PluginConsole /> },
    ],
  },
]);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
