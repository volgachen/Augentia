import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { BrowseResponse } from "../api/client";

interface BrowserState {
  data: BrowseResponse | null;
  loading: boolean;
  error: string | null;
}

export function useBrowser(open: boolean) {
  const [state, setState] = useState<BrowserState>({ data: null, loading: false, error: null });

  const load = useCallback(async (path: string | null) => {
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const data = await api.fs.browse(path);
      setState({ data, loading: false, error: null });
    } catch (e) {
      setState({ data: null, loading: false, error: (e as Error).message });
    }
  }, []);

  useEffect(() => {
    if (open) load(null);
  }, [open, load]);

  return { ...state, load };
}
