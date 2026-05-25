"use client";

import { useState, useEffect, useCallback } from "react";
import type { Worker } from "@/lib/types";
import { fetchWorkers } from "@/lib/api";
import Header from "@/components/Header";
import WorkerList from "@/components/WorkerList";
import WorkerDetail from "@/components/WorkerDetail";
import SpawnModal from "@/components/SpawnModal";

export default function Home() {
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [showSpawn, setShowSpawn] = useState(false);
  const [mobileDetail, setMobileDetail] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const list = await fetchWorkers();
      setWorkers(list);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 10000);
    return () => clearInterval(timer);
  }, [refresh]);

  const handleSelect = (name: string) => {
    setSelected(name);
    setMobileDetail(true);
  };

  const handleBack = () => {
    setSelected(null);
    setMobileDetail(false);
  };

  const handleKilled = () => {
    setSelected(null);
    setMobileDetail(false);
    refresh();
  };

  return (
    <div className="flex flex-col h-dvh">
      <Header />
      <div className="flex-1 flex overflow-hidden">
        {/* Sidebar — hidden on mobile when detail is open */}
        <div
          className={`${mobileDetail ? "hidden md:flex" : "flex"} flex-col`}
        >
          <WorkerList
            workers={workers}
            selected={selected}
            onSelect={handleSelect}
            onSpawn={() => setShowSpawn(true)}
          />
        </div>

        {/* Detail panel */}
        {selected ? (
          <WorkerDetail
            key={selected}
            workerName={selected}
            onKilled={handleKilled}
            onBack={handleBack}
          />
        ) : (
          <div className="flex-1 hidden md:flex flex-col items-center justify-center gap-3 text-text-dim text-sm">
            <svg
              width="48"
              height="48"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="opacity-30"
            >
              <path d="M12 2L2 7l10 5 10-5-10-5z" />
              <path d="M2 17l10 5 10-5" />
              <path d="M2 12l10 5 10-5" />
            </svg>
            Select a worker to view conversation
          </div>
        )}
      </div>

      <SpawnModal
        open={showSpawn}
        onClose={() => setShowSpawn(false)}
        onSpawned={refresh}
      />
    </div>
  );
}
