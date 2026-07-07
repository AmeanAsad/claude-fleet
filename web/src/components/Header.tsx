"use client";

import { useState, useEffect, useCallback } from "react";
import { getStoredToken, setToken } from "@/lib/api";

/**
 * Top status strip — 32px on desktop, 44px on mobile (thumb-safe).
 *
 * When connected, this shrinks to a single monospace line that reads like a
 * terminal status prompt: `● CFLEET · 20.101.80.222 · N workers`. When not
 * connected, it exposes the token affordance inline. The header never grows
 * a "brand" area — the app title is the connected state.
 */
export default function Header({ workerCount = 0 }: { workerCount?: number }) {
  const [tokenInput, setTokenInput] = useState("");
  const [connected, setConnected] = useState(false);
  const [checking, setChecking] = useState(false);
  const [serverHost, setServerHost] = useState<string>("");
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
        const info = (await res.json().catch(() => ({}))) as {
          host?: string;
          server_url?: string;
        };
        setToken(token.trim());
        setServerHost(info.host || info.server_url || window.location.host);
        setConnected(true);
      } else {
        setError("Invalid token");
        setConnected(false);
      }
    } catch {
      setError("Server unreachable");
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

  if (connected) {
    return (
      <div className="rule-b bg-bg shrink-0 h-11 md:h-8 flex items-center px-4 md:px-5">
        <div className="flex items-center gap-2 font-mono text-[11px] md:text-[11px] tracking-wide uppercase text-text-dim">
          <span className="w-1.5 h-1.5 bg-signal shrink-0" aria-hidden />
          <span className="text-text-mid font-medium">cfleet</span>
          <span className="text-rule">·</span>
          <span className="truncate max-w-[180px] md:max-w-none">
            {serverHost || "connected"}
          </span>
          <span className="text-rule hidden sm:inline">·</span>
          <span className="hidden sm:inline tabular-nums">
            {workerCount} {workerCount === 1 ? "worker" : "workers"}
          </span>
        </div>
        <div className="flex-1" />
        <button
          onClick={handleDisconnect}
          className="font-mono text-[10px] uppercase tracking-wider text-text-dim hover:text-text transition-colors cursor-pointer"
          title="Sign out"
        >
          disconnect
        </button>
      </div>
    );
  }

  return (
    <div className="rule-b bg-bg shrink-0 min-h-[52px] flex items-center gap-3 px-4 md:px-5 py-2">
      <span className="font-mono text-[13px] font-medium text-text tracking-tight">
        cfleet
      </span>
      <span className="font-mono text-[10px] uppercase tracking-wider text-text-dim hidden sm:inline">
        · authenticate to continue
      </span>
      <div className="flex-1" />
      <div className="flex items-center gap-2">
        {error && (
          <span className="font-mono text-[11px] text-text hidden sm:inline">
            {error}
          </span>
        )}
        <input
          type="password"
          value={tokenInput}
          onChange={(e) => setTokenInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleConnect()}
          placeholder="Server token"
          autoComplete="off"
          className="font-mono bg-panel text-text px-2.5 py-1.5 text-[13px] w-[180px] focus:outline-none placeholder:text-text-dim border border-text-dim focus:border-signal"
        />
        <button
          onClick={handleConnect}
          disabled={checking || !tokenInput.trim()}
          className="font-mono text-[11px] uppercase tracking-wider px-3 py-1.5 bg-text text-bg hover:bg-signal transition-colors cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed"
        >
          {checking ? "···" : "connect"}
        </button>
      </div>
    </div>
  );
}
