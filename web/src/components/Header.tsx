"use client";

import { useState, useEffect, useCallback } from "react";
import { getStoredToken, setToken } from "@/lib/api";

export default function Header() {
  const [tokenInput, setTokenInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState("");

  const tryConnect = useCallback(async (token: string) => {
    if (!token.trim()) return;
    setChecking(true);
    setError("");
    try {
      const res = await fetch("/api/server/info", {
        headers: { Authorization: `Bearer ${token.trim()}` },
      });
      if (res.ok) {
        setToken(token.trim());
        setConnected(true);
      } else {
        setError("Invalid token");
        setConnected(false);
      }
    } catch {
      setError("Cannot reach server");
      setConnected(false);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    const stored = getStoredToken();
    if (stored) {
      setTokenInput(stored);
      tryConnect(stored);
    }
  }, [tryConnect]);

  const handleConnect = () => tryConnect(tokenInput);

  const handleDisconnect = () => {
    setConnected(false);
    setTokenInput("");
    setToken("");
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") handleConnect();
  };

  return (
    <div className="flex items-center gap-3 px-4 py-2.5 bg-surface border-b border-border shrink-0 min-h-[48px]">
      <div className="flex items-center gap-2 font-bold text-[15px] text-accent-bright tracking-tight whitespace-nowrap">
        <svg
          width="20"
          height="20"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M12 2L2 7l10 5 10-5-10-5z" />
          <path d="M2 17l10 5 10-5" />
          <path d="M2 12l10 5 10-5" />
        </svg>
        <span>Claude Fleet</span>
      </div>
      <div className="flex-1" />
      {connected ? (
        <div className="flex items-center gap-2">
          <span className="flex items-center gap-1.5 text-xs text-green font-medium">
            <span className="w-1.5 h-1.5 rounded-full bg-green animate-pulse" />
            Connected
          </span>
          <button
            onClick={handleDisconnect}
            className="text-[11px] px-2 py-0.5 rounded border border-border text-text-dim hover:text-text hover:border-text transition-all cursor-pointer"
          >
            Reconnect
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-1.5">
          {error && (
            <span className="text-[11px] text-red">{error}</span>
          )}
          <input
            type="password"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Server token"
            autoComplete="off"
            className="bg-surface-2 border border-border text-text px-2.5 py-1 rounded-md text-xs w-[160px] focus:outline-none focus:border-accent-dim"
          />
          <button
            onClick={handleConnect}
            disabled={checking || !tokenInput.trim()}
            className="text-xs px-3 py-1 rounded-md font-medium border border-accent-dim text-accent bg-accent-glow hover:bg-accent-glow-strong hover:border-accent transition-all cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {checking ? "..." : "Join"}
          </button>
        </div>
      )}
    </div>
  );
}
