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
import MessageView from "./MessageView";
import PromptBar from "./PromptBar";

interface Props {
  workerName: string;
  onKilled: () => void;
  onBack: () => void;
}

export default function WorkerDetail({ workerName, onKilled, onBack }: Props) {
  const [detail, setDetail] = useState<Worker | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [showInfo, setShowInfo] = useState(false);
  const [toast, setToast] = useState<{ msg: string; error?: boolean } | null>(
    null,
  );
  const eventSourceRef = useRef<EventSource | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const showToast = (msg: string, error = false) => {
    setToast({ msg, error });
    setTimeout(() => setToast(null), 3000);
  };

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const [info, msgData] = await Promise.all([
          fetchWorker(workerName),
          fetchMessages(workerName),
        ]);
        if (cancelled) return;
        setDetail(info);
        setMessages(msgData.messages || []);
      } catch {
        /* ignore */
      }
    }

    load();

    const source = createLogStream(
      workerName,
      (msg) => {
        if (!cancelled) setMessages((prev) => [...prev, msg]);
      },
      (data) => {
        if (!cancelled && data.idle !== undefined) {
          setDetail((prev) =>
            prev
              ? { ...prev, status: data.idle ? "idle" : "working" }
              : prev,
          );
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

    const msgPoll = setInterval(async () => {
      if (cancelled) return;
      try {
        const data = await fetchMessages(workerName);
        if (!cancelled) {
          setMessages((prev) => {
            const newMsgs = data.messages || [];
            return newMsgs.length > prev.length ? newMsgs : prev;
          });
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
    if (!confirm(`Destroy worker "${workerName}"?`)) return;
    try {
      await killWorker(workerName);
      showToast(`Destroying ${workerName}`);
      onKilled();
    } catch (e: unknown) {
      showToast(`Failed: ${e instanceof Error ? e.message : e}`, true);
    }
  }, [workerName, onKilled]);

  const status = detail?.status || "...";
  const statusBg: Record<string, string> = {
    idle: "bg-green/10 text-green",
    working: "bg-yellow/10 text-yellow",
    spawning: "bg-cyan/10 text-cyan",
    provisioning: "bg-cyan/10 text-cyan",
    errored: "bg-red/10 text-red",
  };

  return (
    <section className="flex flex-col flex-1 overflow-hidden bg-bg">
      {/* Worker bar */}
      <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border bg-surface shrink-0 flex-wrap">
        <button
          onClick={onBack}
          className="text-accent text-sm md:hidden cursor-pointer"
        >
          &larr;
        </button>
        <div className="font-semibold text-sm text-text">{workerName}</div>
        <div
          className={`text-[11px] px-2 py-0.5 rounded-full font-medium ${statusBg[status] || "bg-surface-2 text-text-dim"}`}
        >
          {status}
        </div>
        <div className="flex-1" />
        {detail && (detail.message_count ?? 0) > 0 && (
          <div className="text-[11px] text-text-dim font-mono">
            {detail.message_count} msgs
          </div>
        )}
        <button
          onClick={() => setShowInfo(!showInfo)}
          className="text-xs px-2.5 py-1 rounded-md border border-border text-text-dim hover:border-text hover:text-text transition-all cursor-pointer"
        >
          Info
        </button>
        <button
          onClick={handleInterrupt}
          className="text-xs px-2.5 py-1 rounded-md border border-border text-text-dim hover:border-yellow hover:text-yellow transition-all cursor-pointer"
        >
          Interrupt
        </button>
        <button
          onClick={handleKill}
          className="text-xs px-2.5 py-1 rounded-md border border-border text-text-dim hover:border-red hover:text-red transition-all cursor-pointer"
        >
          Kill
        </button>
      </div>

      {/* Info row */}
      {showInfo && detail && (
        <div className="flex gap-4 flex-wrap px-4 py-2 border-b border-border bg-surface-2 text-xs">
          <InfoChip label="Machine" value={detail.machine_name} />
          <InfoChip label="Provider" value={detail.provider} />
          <InfoChip label="IP" value={detail.machine_ip} />
          <InfoChip label="Port" value={String(detail.relay_port)} />
          <InfoChip label="Model" value={detail.model} />
          <InfoChip label="Session" value={detail.session_id?.slice(0, 8)} />
          <InfoChip
            label="Relay"
            value={
              detail.relay_alive === undefined
                ? undefined
                : detail.relay_alive
                  ? "alive"
                  : "dead"
            }
          />
        </div>
      )}

      {/* Messages */}
      <MessageView messages={messages} />

      {/* Prompt */}
      <PromptBar onSend={handleSend} />

      {/* Toast */}
      {toast && (
        <div
          className={`fixed bottom-5 left-1/2 -translate-x-1/2 px-4 py-2 rounded-lg text-[13px] z-50 border transition-opacity ${
            toast.error
              ? "bg-surface-2 border-red/30 text-red"
              : "bg-surface-2 border-border text-text"
          }`}
        >
          {toast.msg}
        </div>
      )}
    </section>
  );
}

function InfoChip({ label, value }: { label: string; value?: string | null }) {
  if (!value || value === "unknown") return null;
  return (
    <span className="text-text-dim">
      <span className="text-text font-medium">{value}</span> {label}
    </span>
  );
}
