"use client";

import { useEffect, useRef, useState } from "react";
import type { Message, ContentBlock } from "@/lib/types";
import { renderMarkdown, fmtNum, hangTime } from "@/lib/format";

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async (ev) => {
        ev.preventDefault();
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        } catch {
          /* ignore */
        }
      }}
      className="absolute top-2 right-2 font-mono text-[10px] uppercase tracking-wider px-1.5 py-0.5 text-text-dim hover:text-signal bg-bg cursor-pointer transition-colors"
      type="button"
    >
      {copied ? "copied" : "copy"}
    </button>
  );
}

function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  return (
    <div className="relative my-2 group">
      <pre className="bg-panel rule-t rule-b rule-l rule-r overflow-x-auto font-mono text-[12px] leading-relaxed text-text pl-3 pr-14 py-2.5"
        style={{ border: "1px solid var(--color-rule)" }}
      >
        {lang && (
          <div className="font-mono text-[10px] text-text-dim mb-1 uppercase tracking-wider">{lang}</div>
        )}
        <code>{code}</code>
      </pre>
      <CopyButton text={code} />
    </div>
  );
}

function renderRichText(text: string): React.ReactNode {
  const parts: React.ReactNode[] = [];
  const fence = /```(\w*)\n([\s\S]*?)```/g;
  let lastIdx = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = fence.exec(text)) !== null) {
    if (match.index > lastIdx) {
      const chunk = text.slice(lastIdx, match.index);
      parts.push(
        <span
          key={`t${key++}`}
          dangerouslySetInnerHTML={{ __html: renderMarkdown(chunk) }}
        />,
      );
    }
    parts.push(<CodeBlock key={`c${key++}`} lang={match[1] || undefined} code={match[2]} />);
    lastIdx = match.index + match[0].length;
  }
  if (lastIdx < text.length) {
    const chunk = text.slice(lastIdx);
    parts.push(
      <span
        key={`t${key++}`}
        dangerouslySetInnerHTML={{ __html: renderMarkdown(chunk) }}
      />,
    );
  }
  return parts;
}

function ToolUseBlock({ block }: { block: ContentBlock }) {
  const tool = block.tool_name || "?";
  const input = (block.tool_input || {}) as Record<string, string>;
  let cmd = tool;
  if (tool === "Bash") cmd = `$ ${input.command || ""}`;
  else if (["Read", "Write", "Edit"].includes(tool))
    cmd = input.file_path || "";
  else if (["Glob", "Grep"].includes(tool)) cmd = input.pattern || "";

  return (
    <div className="flex items-baseline gap-2 my-1 font-mono text-[12px] text-text-dim min-w-0">
      <span className="font-mono text-[10px] uppercase tracking-wider text-text-mid shrink-0">
        {tool}
      </span>
      <span className="truncate text-text-mid min-w-0 flex-1">{cmd}</span>
    </div>
  );
}

function ToolResultBlock({ block }: { block: ContentBlock }) {
  const content = block.content || "";
  const isErr = block.is_error;
  if (!content && !isErr) return null;

  const len = typeof content === "string" ? content.length : 0;
  const text = typeof content === "string" ? content : String(content);
  const truncated = text.length > 800 ? text.slice(0, 800) + "..." : text;

  return (
    <details className="my-1 font-mono text-[11px]">
      <summary className="cursor-pointer text-text-dim hover:text-text select-none">
        <span className="uppercase tracking-wider">
          {isErr ? "error" : "output"}
        </span>
        <span className="text-rule mx-1.5">·</span>
        <span className="tabular-nums">{fmtNum(len)} chars</span>
      </summary>
      <pre
        className="mt-1.5 p-2.5 bg-panel font-mono text-[11px] leading-snug whitespace-pre-wrap break-all max-h-[240px] overflow-y-auto text-text-mid"
        style={{ border: "1px solid var(--color-rule)" }}
      >
        {truncated}
      </pre>
    </details>
  );
}

function ThinkingBlock({ block }: { block: ContentBlock }) {
  const thinking = block.thinking || "";
  if (!thinking.trim()) return null;
  const preview =
    thinking.length > 80 ? thinking.slice(0, 80) + "..." : thinking;

  return (
    <details className="my-1">
      <summary className="font-mono text-[11px] text-text-dim cursor-pointer select-none hover:text-text">
        <span className="uppercase tracking-wider">thinking</span>
        <span className="text-rule mx-1.5">·</span>
        <span className="italic normal-case tracking-normal">{preview}</span>
      </summary>
      <div className="text-[13px] text-text-dim italic leading-relaxed whitespace-pre-wrap mt-2 max-h-[320px] overflow-y-auto pl-3 rule-l">
        {thinking}
      </div>
    </details>
  );
}

function TextBlock({ text }: { text: string }) {
  if (!text.trim()) return null;
  return (
    <div className="font-sans text-[15px] leading-[1.6] whitespace-pre-wrap break-words text-text [&_code]:font-mono [&_code]:text-[13px] [&_code]:bg-panel [&_code]:px-1 [&_code]:py-px">
      {renderRichText(text)}
    </div>
  );
}

function ContentBlockView({ block }: { block: ContentBlock }) {
  if (block.type === "TextBlock") return <TextBlock text={block.text || ""} />;
  if (block.type === "ThinkingBlock") return <ThinkingBlock block={block} />;
  if (block.type === "ToolUseBlock") return <ToolUseBlock block={block} />;
  if (block.type === "ToolResultBlock")
    return <ToolResultBlock block={block} />;
  return null;
}

/**
 * A single message in the stream.
 *
 * Layout is a two-column grid: a 56px tabular-monospace gutter on the left
 * for the timestamp (hanging like an ATC log), and the message body on the
 * right. This is the structural device that makes the stream feel like an
 * instrumentation readout rather than a chat app. On mobile the gutter
 * shrinks to 44px and the timestamp becomes optional-hover.
 *
 * User vs assistant is differentiated by an eyebrow label and body weight —
 * not by chat-bubble alignment, which is the template answer. Both stack
 * left-aligned in one column so long transcripts don't get zig-zag fatigue.
 */
function MessageRow({ msg }: { msg: Message }) {
  if (msg.type === "SystemMessage" && msg.subtype === "init") return null;
  if (msg.role === "result" || msg.type === "ResultMessage") return null;

  const content = msg.content;
  const time = hangTime(msg.timestamp);

  if (msg.role === "user") {
    const blocks = Array.isArray(content) ? content : [];
    const text = blocks
      .filter((b) => b.type === "TextBlock")
      .map((b) => b.text || "")
      .join("\n");
    const displayText = typeof content === "string" ? content : text;
    if (!displayText.trim()) return null;

    return (
      <div
        className="grid grid-cols-[44px_1fr] md:grid-cols-[56px_1fr] gap-x-2 md:gap-x-4 py-3 items-start animate-[fadeIn_0.2s_ease] rule-b"
      >
        <div className="font-mono text-[11px] text-text-dim tabular-nums pt-1 text-right">
          {time}
        </div>
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-text-dim mb-1">
            you
          </div>
          <div className="font-sans text-[15px] leading-[1.55] text-text whitespace-pre-wrap break-words">
            {displayText}
          </div>
        </div>
      </div>
    );
  }

  if (msg.role === "assistant") {
    const blocks = Array.isArray(content) ? content : [];
    if (blocks.length === 0) return null;
    return (
      <div
        className="grid grid-cols-[44px_minmax(0,1fr)] md:grid-cols-[56px_minmax(0,1fr)] gap-x-2 md:gap-x-4 py-3 items-start animate-[fadeIn_0.2s_ease] rule-b"
      >
        <div className="font-mono text-[11px] text-text-dim tabular-nums pt-1 text-right">
          {time}
        </div>
        <div className="min-w-0">
          <div className="font-mono text-[10px] uppercase tracking-widest text-signal mb-1">
            agent
          </div>
          <div className="space-y-1 min-w-0">
            {blocks.map((b, i) => (
              <ContentBlockView key={i} block={b} />
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (msg.role === "system") {
    const blocks = Array.isArray(content) ? content : [];
    const text =
      typeof content === "string"
        ? content
        : blocks
            .filter((b) => b.type === "TextBlock")
            .map((b) => b.text || "")
            .join("\n");
    if (!text.trim()) return null;
    return (
      <div className="grid grid-cols-[44px_1fr] md:grid-cols-[56px_1fr] gap-x-2 md:gap-x-4 py-2 items-baseline">
        <div className="font-mono text-[11px] text-text-dim tabular-nums text-right">
          {time}
        </div>
        <div className="font-mono text-[11px] text-text-dim italic">
          {text}
        </div>
      </div>
    );
  }

  return null;
}

interface Props {
  messages: Message[];
  working?: boolean;
  loading?: boolean;
  hasMore?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void;
}

export default function MessageView({
  messages,
  working,
  loading,
  hasMore,
  loadingOlder,
  onLoadOlder,
}: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  // Track the count/last-message-key so we can distinguish "new message
  // arrived (scroll to bottom)" from "older-history prepended (preserve
  // viewport position)".
  const prevCountRef = useRef<number>(0);
  const prevFirstKeyRef = useRef<string>("");
  const prevScrollHeightRef = useRef<number>(0);

  const firstKey = messages[0]?.timestamp || `${messages.length}`;

  // Before render, capture scrollHeight so we can restore after a prepend.
  // useLayoutEffect (via useEffect running before paint via ref pattern) —
  // React re-runs effects after commit, but we snapshot at each render for
  // the subsequent compare.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;

    const prevCount = prevCountRef.current;
    const prevFirst = prevFirstKeyRef.current;
    const prevScrollHeight = prevScrollHeightRef.current;
    prevCountRef.current = messages.length;
    prevFirstKeyRef.current = firstKey;
    prevScrollHeightRef.current = el.scrollHeight;

    if (prevCount === 0 && messages.length > 0) {
      // Initial load — jump straight to bottom without smooth-scroll to
      // avoid a visible scroll animation on mount.
      el.scrollTop = el.scrollHeight;
      return;
    }
    if (messages.length > prevCount && firstKey === prevFirst) {
      // New message appended (SSE stream or poll). Smooth-scroll to bottom.
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
      return;
    }
    if (messages.length > prevCount && firstKey !== prevFirst) {
      // Older history prepended. Preserve the user's reading position by
      // restoring the previous scroll offset from the new bottom.
      const delta = el.scrollHeight - prevScrollHeight;
      el.scrollTop = el.scrollTop + delta;
      return;
    }
    // Working spinner appears/disappears — keep scroll pinned to bottom if
    // we were already near it.
    if (working) {
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 200;
      if (nearBottom) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages.length, firstKey, working]);

  if (loading) {
    return (
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-[560px] mx-auto px-4 md:px-6 py-16 md:py-24">
          <div className="font-eyebrow mb-3 animate-signal">Loading stream</div>
          <div className="font-mono text-[12px] text-text-dim leading-relaxed">
            Fetching the most recent 200 messages from the session.
          </div>
        </div>
      </div>
    );
  }

  if (messages.length === 0 && !working) {
    return (
      <div className="flex-1 overflow-y-auto">
        <div className="max-w-[560px] mx-auto px-4 md:px-6 py-12 md:py-20">
          <div className="font-eyebrow mb-3">Stream · empty</div>
          <div className="font-sans text-[16px] text-text mb-2 leading-snug">
            No messages yet.
          </div>
          <div className="font-mono text-[12px] text-text-dim leading-relaxed">
            Send a prompt to bring the agent online.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div ref={scrollRef} className="flex-1 overflow-y-auto">
      <div className="max-w-[860px] mx-auto px-4 md:px-6">
        {/* Older-history affordance — sits at the top of the loaded window.
            The reader guarantees `hasMore` reflects file-level truth, so this
            hides itself once we've loaded everything. */}
        {hasMore && (
          <div className="py-4 flex justify-center rule-b">
            <button
              onClick={onLoadOlder}
              disabled={loadingOlder}
              className="font-mono text-[11px] uppercase tracking-wider text-text-dim hover:text-signal transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {loadingOlder ? "loading···" : "↑ load older"}
            </button>
          </div>
        )}

        {messages.map((msg, i) => (
          <MessageRow key={i} msg={msg} />
        ))}
        {working && (
          <div className="grid grid-cols-[44px_1fr] md:grid-cols-[56px_1fr] gap-x-2 md:gap-x-4 py-3 items-baseline animate-[fadeIn_0.2s_ease]">
            <div className="font-mono text-[11px] text-signal tabular-nums text-right animate-signal">
              live
            </div>
            <div className="font-mono text-[12px] text-text-dim">
              <span className="text-signal">▪</span>{" "}
              <span className="italic">agent is working</span>
            </div>
          </div>
        )}
        <div ref={bottomRef} className="h-4" />
      </div>
    </div>
  );
}
