"use client";

import { useState, useEffect, useCallback } from "react";
import type { Worker } from "@/lib/types";
import { fetchWorkers } from "@/lib/api";
import Header from "@/components/Header";
import WorkerList from "@/components/WorkerList";
import WorkerDetail from "@/components/WorkerDetail";
import SpawnModal from "@/components/SpawnModal";

/**
 * Shell — two panels, one row.
 *
 * Mobile: list and detail are swap-in-place (no drawer, no bottom-sheet
 * animation for switching workers — the operator is triaging, not browsing).
 * Desktop: fixed 300px roster on the left, message stream on the right.
 * The URL-less state means shareable-links don't work; that's a deliberate
 * tradeoff — this is a console, not a wiki. Add hash routing later if
 * needed.
 */
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
    <div className="flex flex-col h-dvh bg-bg">
      <Header workerCount={workers.length} />

      <div className="flex-1 flex overflow-hidden">
        {/* Fleet roster */}
        <div
          className={`${mobileDetail ? "hidden md:flex" : "flex"} flex-col flex-1 md:flex-none`}
        >
          <WorkerList
            workers={workers}
            selected={selected}
            onSelect={handleSelect}
            onSpawn={() => setShowSpawn(true)}
          />
        </div>

        {/* Detail */}
        {selected ? (
          <WorkerDetail
            key={selected}
            workerName={selected}
            onKilled={handleKilled}
            onBack={handleBack}
          />
        ) : (
          <div className="hidden md:flex flex-1 flex-col items-center justify-center">
            <div className="max-w-[420px] px-6">
              <div className="font-eyebrow mb-3">Standby</div>
              <div className="font-callsign text-[22px] text-text mb-3 leading-tight">
                Pick a worker from the roster.
              </div>
              <div className="font-mono text-[12px] text-text-dim leading-relaxed">
                Every worker persists on its host across sessions. Attach from
                any laptop with{" "}
                <span className="text-text">cfleet attach</span> — the console
                and CLI are peers on the same stream.
              </div>
            </div>
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
