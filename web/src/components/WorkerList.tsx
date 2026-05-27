"use client";

import type { Worker } from "@/lib/types";
import { dotColor } from "@/lib/format";

interface Props {
  workers: Worker[];
  selected: string | null;
  onSelect: (name: string) => void;
  onSpawn: () => void;
}

export default function WorkerList({
  workers,
  selected,
  onSelect,
  onSpawn,
}: Props) {
  return (
    <aside className="flex flex-col border-r border-border bg-surface overflow-hidden w-[280px] shrink-0">
      <div className="flex items-center justify-between px-4 pt-3.5 pb-2">
        <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-text-dim">
          Workers
        </span>
        <button
          onClick={onSpawn}
          className="text-[11px] px-2.5 py-0.5 rounded font-medium bg-accent-bright text-white hover:bg-accent transition-all cursor-pointer"
        >
          + Spawn
        </button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 py-1">
        {workers.length === 0 && (
          <div className="text-text-dim text-xs px-3 py-4 italic">
            No workers running
          </div>
        )}
        {workers.map((w) => (
          <button
            key={w.name}
            onClick={() => onSelect(w.name)}
            className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg cursor-pointer text-left transition-colors border ${
              selected === w.name
                ? "bg-surface-2 border-border-light"
                : "border-transparent hover:bg-surface-2"
            }`}
          >
            <div
              className={`w-2 h-2 rounded-full shrink-0 ${dotColor(w.status)}`}
            />
            <div className="flex-1 min-w-0">
              <div className="text-[13px] font-medium text-text truncate">
                {w.name}
              </div>
              <div className="text-[11px] text-text-dim mt-px">
                {w.status}
                {w.machine_name ? ` · ${w.machine_name}` : ""}
              </div>
            </div>
          </button>
        ))}
      </div>

      <div className="px-4 py-2.5 border-t border-border text-[11px] text-text-dim">
        {workers.length > 0 ? (
          <>
            <span className="text-text font-medium">{workers.length}</span>{" "}
            worker{workers.length !== 1 ? "s" : ""}
          </>
        ) : (
          "No workers"
        )}
      </div>
    </aside>
  );
}
