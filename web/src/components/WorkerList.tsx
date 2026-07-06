"use client";

import type { Worker } from "@/lib/types";
import { statusGlyph, relTime } from "@/lib/format";

interface Props {
  workers: Worker[];
  selected: string | null;
  onSelect: (name: string) => void;
  onSpawn: () => void;
}

/**
 * Fleet roster — the primary navigation surface.
 *
 * Every row is a callsign. Left gutter is the status glyph (monospace, single
 * character, colored only when the worker is actively working). Right gutter
 * is the last-activity time in tabular monospace so you can scan tempo down
 * the right edge. Middle carries the worker name (mono, uppercase-adjacent
 * treatment) and the machine it lives on (small, dim, structural).
 *
 * No cards, no rounded corners, no shadows. Rows sit against hairline rules
 * because the operator is triaging state, not browsing a catalog.
 */
export default function WorkerList({
  workers,
  selected,
  onSelect,
  onSpawn,
}: Props) {
  return (
    <aside className="flex flex-col rule-r bg-bg overflow-hidden md:w-[300px] shrink-0 flex-1 md:flex-none">
      {/* Section header — eyebrow + count + spawn action */}
      <div className="flex items-center px-4 py-3 rule-b shrink-0">
        <span className="font-eyebrow">
          Fleet
        </span>
        <span className="font-mono text-[11px] text-text-dim ml-2 tabular-nums">
          {workers.length}
        </span>
        <div className="flex-1" />
        <button
          onClick={onSpawn}
          className="font-mono text-[11px] uppercase tracking-wider text-text hover:text-signal transition-colors cursor-pointer"
        >
          + spawn
        </button>
      </div>

      {/* Roster */}
      <div className="flex-1 overflow-y-auto">
        {workers.length === 0 && (
          <div className="px-4 py-6">
            <div className="font-mono text-[12px] text-text-dim leading-relaxed">
              <span className="text-text">Roster empty.</span>
              <br />
              Spawn a worker to bring the fleet online.
            </div>
          </div>
        )}
        {workers.map((w) => {
          const glyph = statusGlyph(w);
          const isSelected = selected === w.name;
          return (
            <button
              key={w.name}
              onClick={() => onSelect(w.name)}
              className={`
                w-full text-left rule-b group
                grid grid-cols-[24px_1fr_auto] items-center gap-3
                pl-4 pr-5 py-3 min-h-[56px] cursor-pointer
                transition-colors
                ${isSelected
                  ? "bg-panel"
                  : "hover:bg-panel/60"}
              `}
            >
              {/* Status glyph — the signature column */}
              <span
                className={`
                  font-mono text-[15px] leading-none
                  ${glyph.className}
                  ${glyph.animate ? "animate-signal" : ""}
                `}
                aria-label={w.status}
              >
                {glyph.glyph}
              </span>

              {/* Callsign + host */}
              <div className="min-w-0">
                <div className="font-callsign text-[14px] text-text truncate leading-tight">
                  {w.name}
                </div>
                <div className="font-mono text-[11px] text-text-dim truncate leading-tight mt-0.5">
                  {w.machine_name || "—"}
                  {w.status !== "idle" && w.status !== "ready" && (
                    <>
                      <span className="text-rule mx-1">·</span>
                      <span className="text-text-mid">{w.status}</span>
                    </>
                  )}
                </div>
              </div>

              {/* Last activity — tabular */}
              <div className="font-meta text-right tabular-nums shrink-0">
                {relTime(w.last_prompt_at || w.created_at)}
              </div>
            </button>
          );
        })}
      </div>
    </aside>
  );
}
