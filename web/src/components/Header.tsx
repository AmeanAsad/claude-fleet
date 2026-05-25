"use client";

import { useState, useEffect } from "react";
import { getStoredToken, setToken } from "@/lib/api";

export default function Header() {
  const [tokenValue, setTokenValue] = useState("");

  useEffect(() => {
    setTokenValue(getStoredToken());
  }, []);

  const handleTokenChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    setTokenValue(e.target.value);
    setToken(e.target.value);
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
      <input
        type="password"
        value={tokenValue}
        onChange={handleTokenChange}
        placeholder="API token"
        autoComplete="off"
        className="bg-surface-2 border border-border text-text px-2.5 py-1 rounded-md text-xs w-[140px] focus:outline-none focus:border-accent-dim"
      />
    </div>
  );
}
