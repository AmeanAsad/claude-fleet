"use client";

import { useEffect, useRef } from "react";
import type { Message, ContentBlock } from "@/lib/types";
import { esc, renderMarkdown, fmtNum } from "@/lib/format";

function ToolUseBlock({ block }: { block: ContentBlock }) {
  const tool = block.tool_name || "?";
  const input = (block.tool_input || {}) as Record<string, string>;
  let cmd = tool;
  if (tool === "Bash") cmd = `$ ${input.command || ""}`;
  else if (["Read", "Write", "Edit"].includes(tool))
    cmd = input.file_path || "";
  else if (["Glob", "Grep"].includes(tool)) cmd = input.pattern || "";

  return (
    <div className="flex items-center gap-1.5 my-1 px-2.5 py-1.5 bg-surface-2 border border-border rounded-md font-mono text-xs overflow-hidden">
      <span className="text-cyan font-semibold shrink-0">{tool}</span>
      <span className="text-text-dim truncate">{cmd}</span>
    </div>
  );
}

function ToolResultBlock({ block }: { block: ContentBlock }) {
  const content = block.content || "";
  const isErr = block.is_error;
  if (!content && !isErr) return null;

  const len = typeof content === "string" ? content.length : 0;
  const truncated =
    typeof content === "string"
      ? content.length > 800
        ? content.slice(0, 800) + "..."
        : content
      : String(content);

  return (
    <details
      className={`my-0.5 text-xs ${isErr ? "text-red" : ""}`}
    >
      <summary
        className={`cursor-pointer font-mono text-[11px] ${isErr ? "text-red" : "text-text-dim"} hover:text-text`}
      >
        {isErr ? "Error" : "Output"} ({fmtNum(len)} chars)
      </summary>
      <pre
        className={`mt-1 p-2 rounded font-mono text-[11px] leading-snug whitespace-pre-wrap break-all max-h-[200px] overflow-y-auto ${
          isErr
            ? "bg-surface border border-red/20 text-red"
            : "bg-surface border border-border text-text-dim"
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
    <div
      className="text-sm leading-relaxed whitespace-pre-wrap break-words [&_code]:bg-surface-3 [&_code]:px-1 [&_code]:py-px [&_code]:rounded [&_code]:font-mono [&_code]:text-xs [&_pre]:bg-surface-2 [&_pre]:border [&_pre]:border-border [&_pre]:rounded-md [&_pre]:p-2.5 [&_pre]:my-2 [&_pre]:overflow-x-auto [&_pre]:font-mono [&_pre]:text-xs [&_pre]:leading-relaxed [&_pre_code]:bg-transparent [&_pre_code]:p-0"
      dangerouslySetInnerHTML={{ __html: renderMarkdown(text) }}
    />
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
      <div className="border-l-[3px] border-accent pl-3 my-2 animate-[fadeIn_0.2s_ease]">
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
}

export default function MessageView({ messages }: Props) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  if (messages.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-text-dim text-sm">
        No messages yet
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 py-4 flex flex-col gap-1">
      {messages.map((msg, i) => (
        <MessageRow key={i} msg={msg} />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
