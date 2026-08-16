import { useEffect } from "react";
import Login from "../pages/Login";
import { useAuthStore } from "../store/auth";

export default function AuthGuard({ children }: { children: React.ReactNode }) {
  const status = useAuthStore((state) => state.status);
  const check = useAuthStore((state) => state.check);

  useEffect(() => {
    void check();
    const handleUnauthorized = () => useAuthStore.setState({ status: "anonymous" });
    window.addEventListener("augentia:unauthorized", handleUnauthorized);
    return () => window.removeEventListener("augentia:unauthorized", handleUnauthorized);
  }, [check]);

  if (status === "checking") {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-950 text-sm text-gray-500">
        Checking authentication…
      </div>
    );
  }

  if (status === "anonymous") {
    return <Login />;
  }

  return <>{children}</>;
}
