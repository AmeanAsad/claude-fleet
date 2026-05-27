"use client";

import { useEffect, useRef, useState } from "react";
import type { Message, ContentBlock } from "@/lib/types";
import { renderMarkdown, fmtNum } from "@/lib/format";

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
      className="absolute top-1.5 right-1.5 text-[10px] px-1.5 py-0.5 rounded border border-border text-text-dim hover:border-border-light hover:text-text bg-surface/90 cursor-pointer transition-colors"
      type="button"
    >
      {copied ? "copied" : "copy"}
    </button>
  );
}

function CodeBlock({ code, lang }: { code: string; lang?: string }) {
  return (
    <div className="relative my-2 group">
      <pre className="bg-surface-2 border border-border rounded p-3 pr-14 overflow-x-auto font-mono text-[12px] leading-relaxed text-text">
        {lang && (
          <div className="text-[10px] text-text-dim mb-1.5 uppercase tracking-wider font-sans">{lang}</div>
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
    <div className="flex items-center gap-1.5 my-0.5 pl-2 border-l border-border font-mono text-[11px] text-text-dim overflow-hidden">
      <span className="shrink-0 text-[10px] uppercase tracking-wide font-sans font-medium">{tool}</span>
      <span className="truncate">{cmd}</span>
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
    <details className={`my-0.5 pl-2 border-l border-border text-[11px] ${isErr ? "text-red" : ""}`}>
      <summary
        className={`cursor-pointer font-mono ${isErr ? "text-red" : "text-text-dim"} hover:text-text select-none`}
      >
        {isErr ? "error" : "output"} · {fmtNum(len)} chars
      </summary>
      <pre
        className={`mt-1 p-2 rounded font-mono text-[11px] leading-snug whitespace-pre-wrap break-all max-h-[200px] overflow-y-auto ${
          isErr
            ? "bg-red/5 border border-red/20 text-red"
            : "bg-surface-2 border border-border text-text-dim"
        }`}
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
    <details className="my-1 border-l-2 border-border pl-2.5">
      <summary className="text-[11px] text-text-dim cursor-pointer select-none italic hover:text-text">
        Thinking: {preview}
      </summary>
      <div className="text-xs text-text-dim italic leading-relaxed whitespace-pre-wrap mt-1 max-h-[300px] overflow-y-auto">
        {thinking}
      </div>
    </details>
  );
}

function TextBlock({ text }: { text: string }) {
  if (!text.trim()) return null;
  return (
    <div className="text-sm leading-relaxed whitespace-pre-wrap break-words [&_code]:bg-surface-3 [&_code]:px-1 [&_code]:py-px [&_code]:rounded [&_code]:font-mono [&_code]:text-xs">
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

function MessageRow({ msg }: { msg: Message }) {
  if (msg.type === "SystemMessage" && msg.subtype === "init") return null;
  if (msg.role === "result" || msg.type === "ResultMessage") return null;

  const content = msg.content;

  if (msg.role === "user") {
    const blocks = Array.isArray(content) ? content : [];
    const text = blocks
      .filter((b) => b.type === "TextBlock")
      .map((b) => b.text || "")
      .join("\n");
    if (!text && typeof content === "string" && !content.trim()) return null;

    return (
      <div className="border-l-2 border-text pl-3.5 my-2 animate-[fadeIn_0.2s_ease]">
        <div className="text-sm text-text leading-relaxed whitespace-pre-wrap break-words">
          {typeof content === "string" ? content : text}
        </div>
      </div>
    );
  }

  if (msg.role === "assistant") {
    const blocks = Array.isArray(content) ? content : [];
    if (blocks.length === 0) return null;
    return (
      <div className="py-1 animate-[fadeIn_0.2s_ease]">
        {blocks.map((b, i) => (
          <ContentBlockView key={i} block={b} />
        ))}
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
      <div className="text-[11px] text-text-dim py-0.5 italic">{text}</div>
    );
  }

  return null;
}

interface Props {
  messages: Message[];
  working?: boolean;
}

export default function MessageView({ messages, working }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, working]);

  if (messages.length === 0 && !working) {
    return (
      <div className="flex-1 flex items-center justify-center text-text-dim text-sm italic">
        No messages yet
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-5 py-4 flex flex-col gap-1">
      {messages.map((msg, i) => (
        <MessageRow key={i} msg={msg} />
      ))}
      {working && (
        <div className="flex items-center gap-2 py-2 text-[12px] text-text-dim animate-[fadeIn_0.2s_ease]">
          <span className="flex gap-1">
            <span className="w-1 h-1 rounded-full bg-text-dim animate-[pulse_1.2s_ease-in-out_infinite]" />
            <span className="w-1 h-1 rounded-full bg-text-dim animate-[pulse_1.2s_ease-in-out_0.2s_infinite]" />
            <span className="w-1 h-1 rounded-full bg-text-dim animate-[pulse_1.2s_ease-in-out_0.4s_infinite]" />
          </span>
          <span className="italic">thinking</span>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
}
