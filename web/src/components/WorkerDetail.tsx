"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import type { Worker, Message } from "@/lib/types";
import {
  fetchWorker,
  fetchMessages,
  sendPrompt,
  interruptWorker,
  killWorker,
  createLogStream,
} from "@/lib/api";
import { statusGlyph } from "@/lib/format";
import MessageView from "./MessageView";
import PromptBar from "./PromptBar";

interface Props {
  workerName: string;
  onKilled: () => void;
  onBack: () => void;
}

function messageKey(m: Message): string {
  // Include timestamp for stability — a user could send the same prompt twice
  // ("ok", "ok"), or the SDK could re-emit a TextBlock the JSONL already has
  // with slightly different envelope. Timestamp collapses those edge cases.
  const ts = m.timestamp || "";
  const blocks = Array.isArray(m.content) ? m.content : [];
  let text = "";
  for (const b of blocks) {
    if (b.type === "TextBlock" && b.text) {
      // Use a snippet + length: full text can be >100KB and blows up the Set.
      text = `text:${b.text.length}:${b.text.slice(0, 120)}`;
      break;
    }
    if (b.type === "ToolUseBlock" && b.tool_id) {
      text = `tool:${b.tool_id}`;
      break;
    }
    if (b.type === "ToolResultBlock" && b.tool_id) {
      text = `result:${b.tool_id}`;
      break;
    }
    if (b.type === "ThinkingBlock" && b.thinking) {
      text = `thinking:${b.thinking.slice(0, 80)}`;
      break;
    }
  }
  return `${ts}|${m.role}|${m.type}|${text}`;
}

function appendUnique(prev: Message[], msg: Message): Message[] {
  const key = messageKey(msg);
  for (let i = prev.length - 1; i >= 0; i--) {
    if (messageKey(prev[i]) === key) return prev;
  }
  return [...prev, msg];
}

function mergeUnique(prev: Message[], incoming: Message[]): Message[] {
  if (incoming.length === 0) return prev;
  const seen = new Set(prev.map(messageKey));
  const out = [...prev];
  for (const m of incoming) {
    const k = messageKey(m);
    if (!seen.has(k)) {
      seen.add(k);
      out.push(m);
    }
  }
  return out;
}

/** Prepend an older-history page in front of the existing timeline, deduping
 *  against what we already have. Used by the "Load older" affordance. */
function prependUnique(prev: Message[], older: Message[]): Message[] {
  if (older.length === 0) return prev;
  const seen = new Set(prev.map(messageKey));
  const head: Message[] = [];
  for (const m of older) {
    const k = messageKey(m);
    if (!seen.has(k)) {
      seen.add(k);
      head.push(m);
    }
  }
  return [...head, ...prev];
}

/**
 * Worker detail — the primary work surface.
 *
 * Header: back chevron (mobile only) · status glyph · callsign · machine ·
 * quiet action row. The header is the one place proportional weight goes;
 * everything below is monospace metadata + prose messages.
 *
 * Info panel is a toggleable strip of key:value pairs, not a modal. Kill and
 * Interrupt are text buttons that go signal-green on hover — no colored
 * "Danger" pills, because red destroys the palette.
 */
export default function WorkerDetail({ workerName, onKilled, onBack }: Props) {
  const [detail, setDetail] = useState<Worker | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [head, setHead] = useState<number>(0);
  const [hasMore, setHasMore] = useState<boolean>(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [showInfo, setShowInfo] = useState(false);
  const [toast, setToast] = useState<{ msg: string; error?: boolean } | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const showToast = (msg: string, error = false) => {
    setToast({ msg, error });
    setTimeout(() => setToast(null), 3000);
  };

  useEffect(() => {
    let cancelled = false;
    // Reset state on worker switch — otherwise stale messages from the previous
    // worker briefly flash while the new tail loads.
    setLoading(true);
    setMessages([]);
    setHead(0);
    setHasMore(false);
    setDetail(null);

    async function load() {
      try {
        const [info, page] = await Promise.all([
          fetchWorker(workerName),
          fetchMessages(workerName, { limit: 200 }),
        ]);
        if (cancelled) return;
        setDetail(info);
        setMessages(page.messages);
        setHead(page.head);
        setHasMore(page.has_more);
      } catch {
        /* ignore */
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();

    const source = createLogStream(
      workerName,
      (msg) => {
        if (!cancelled) setMessages((prev) => appendUnique(prev, msg));
      },
      (data) => {
        if (cancelled) return;
        const next = (data.status ?? (data.idle !== undefined ? (data.idle ? "idle" : "working") : undefined)) as string | undefined;
        if (next) {
          setDetail((prev) => (prev ? { ...prev, status: next } : prev));
        }
      },
    );
    eventSourceRef.current = source;

    pollRef.current = setInterval(async () => {
      if (cancelled) return;
      try {
        const info = await fetchWorker(workerName);
        if (!cancelled) setDetail(info);
      } catch {
        /* ignore */
      }
    }, 5000);

    // Message tail poll — safety-net for missed SSE events. Always fetches the
    // tail (last 200); mergeUnique dedups against what we already have.
    const msgPoll = setInterval(async () => {
      if (cancelled) return;
      try {
        const page = await fetchMessages(workerName, { limit: 200 });
        if (!cancelled) {
          setMessages((prev) => mergeUnique(prev, page.messages));
          // Note: don't touch `head` from the tail poll — that only advances
          // when the user actively pages older, and this could roll it back.
        }
      } catch {
        /* ignore */
      }
    }, 8000);

    return () => {
      cancelled = true;
      source.close();
      eventSourceRef.current = null;
      if (pollRef.current) clearInterval(pollRef.current);
      clearInterval(msgPoll);
    };
  }, [workerName]);

  const handleLoadOlder = useCallback(async () => {
    if (loadingOlder || !hasMore || head <= 0) return;
    setLoadingOlder(true);
    try {
      const page = await fetchMessages(workerName, {
        limit: 200,
        before: head,
      });
      setMessages((prev) => prependUnique(prev, page.messages));
      setHead(page.head);
      setHasMore(page.has_more);
    } catch (e: unknown) {
      showToast(`Load older failed: ${e instanceof Error ? e.message : e}`, true);
    } finally {
      setLoadingOlder(false);
    }
  }, [workerName, head, hasMore, loadingOlder]);

  const handleSend = useCallback(
    async (prompt: string) => {
      const optimisticMsg: Message = {
        type: "UserPrompt",
        role: "user",
        timestamp: new Date().toISOString(),
        content: [{ type: "TextBlock", text: prompt }],
      };
      setMessages((prev) => [...prev, optimisticMsg]);
      try {
        await sendPrompt(workerName, prompt);
      } catch (e: unknown) {
        showToast(`Failed: ${e instanceof Error ? e.message : e}`, true);
      }
    },
    [workerName],
  );

  const handleInterrupt = useCallback(async () => {
    try {
      await interruptWorker(workerName);
      showToast(`Interrupted ${workerName}`);
    } catch (e: unknown) {
      showToast(`Failed: ${e instanceof Error ? e.message : e}`, true);
    }
  }, [workerName]);

  const handleKill = useCallback(async () => {
    if (!confirm(`Destroy worker "${workerName}"? This is not reversible.`)) return;
    try {
      await killWorker(workerName);
      showToast(`Destroying ${workerName}`);
      onKilled();
    } catch (e: unknown) {
      showToast(`Failed: ${e instanceof Error ? e.message : e}`, true);
    }
  }, [workerName, onKilled]);

  const glyph = detail ? statusGlyph(detail) : { glyph: "·", animate: false, className: "text-text-dim" };
  const status = detail?.status || "connecting";

  return (
    <section className="flex flex-col flex-1 overflow-hidden bg-bg">
      {/* Callsign bar */}
      <div className="rule-b bg-bg shrink-0">
        <div className="flex items-center gap-3 px-4 md:px-5 py-2.5">
          <button
            onClick={onBack}
            className="md:hidden font-mono text-[16px] leading-none text-text-dim hover:text-text cursor-pointer -ml-1 p-1"
            aria-label="Back to fleet"
          >
            ←
          </button>

          <span
            className={`
              font-mono text-[18px] leading-none shrink-0
              ${glyph.className}
              ${glyph.animate ? "animate-signal" : ""}
            `}
            aria-label={status}
          >
            {glyph.glyph}
          </span>

          <div className="min-w-0 flex-1">
            <div className="font-callsign text-[16px] md:text-[18px] text-text truncate leading-tight">
              {workerName}
            </div>
            <div className="font-mono text-[11px] text-text-dim truncate leading-tight mt-0.5 uppercase tracking-wider">
              {detail?.machine_name || "—"}
              <span className="text-rule mx-1.5">·</span>
              <span className="text-text-mid">{status}</span>
              {detail?.model && (
                <>
                  <span className="text-rule mx-1.5">·</span>
                  <span className="normal-case tracking-normal">{detail.model.replace("claude-", "")}</span>
                </>
              )}
            </div>
          </div>

          <div className="flex items-center gap-3 shrink-0">
            <button
              onClick={() => setShowInfo(!showInfo)}
              className="font-mono text-[11px] uppercase tracking-wider text-text-dim hover:text-text transition-colors cursor-pointer"
            >
              info
            </button>
            <button
              onClick={handleInterrupt}
              className="font-mono text-[11px] uppercase tracking-wider text-text-dim hover:text-signal transition-colors cursor-pointer hidden sm:inline"
            >
              stop
            </button>
            <button
              onClick={handleKill}
              className="font-mono text-[11px] uppercase tracking-wider text-text-dim hover:text-text transition-colors cursor-pointer"
            >
              kill
            </button>
          </div>
        </div>

        {/* Info strip — a single monospace row of key:value pairs, no chips. */}
        {showInfo && detail && (
          <div className="rule-t bg-panel px-4 md:px-5 py-2 overflow-x-auto">
            <div className="flex gap-x-5 gap-y-1 flex-wrap font-mono text-[11px]">
              <InfoField label="host" value={detail.machine_ip || detail.machine_name} />
              <InfoField label="port" value={String(detail.relay_port)} />
              <InfoField label="model" value={detail.model?.replace("claude-", "")} />
              <InfoField label="session" value={detail.session_id?.slice(0, 8)} />
              <InfoField label="provider" value={detail.provider} />
              <InfoField
                label="relay"
                value={detail.relay_alive === undefined ? undefined : detail.relay_alive ? "alive" : "dead"}
                muted={detail.relay_alive === false}
              />
              <InfoField
                label="msgs"
                value={detail.message_count !== undefined ? String(detail.message_count) : undefined}
              />
              {detail.total_cost_usd !== undefined && (
                <InfoField
                  label="cost"
                  value={`$${detail.total_cost_usd.toFixed(3)}`}
                />
              )}
            </div>
          </div>
        )}
      </div>

      {/* Message stream */}
      <MessageView
        messages={messages}
        working={status === "working"}
        loading={loading}
        hasMore={hasMore}
        loadingOlder={loadingOlder}
        onLoadOlder={handleLoadOlder}
      />

      {/* Prompt */}
      <PromptBar onSend={handleSend} disabled={status === "spawning" || status === "provisioning"} />

      {/* Toast — restrained, matches system voice */}
      {toast && (
        <div className="fixed bottom-24 md:bottom-20 left-1/2 -translate-x-1/2 z-50 px-4 py-2 bg-text text-bg font-mono text-[12px] uppercase tracking-wider">
          {toast.msg}
        </div>
      )}
    </section>
  );
}

function InfoField({ label, value, muted }: { label: string; value?: string | null; muted?: boolean }) {
  if (!value || value === "unknown") return null;
  return (
    <span className="whitespace-nowrap">
      <span className="text-text-dim uppercase tracking-wider">{label}</span>
      <span className="text-rule mx-1.5">/</span>
      <span className={muted ? "text-text-dim" : "text-text"}>{value}</span>
    </span>
  );
}
