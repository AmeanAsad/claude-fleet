"use client";

import { useState, useRef, useCallback } from "react";

interface Props {
  onSend: (prompt: string) => void;
  disabled?: boolean;
}

export default function PromptBar({ onSend, disabled }: Props) {
  const [value, setValue] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSend = useCallback(() => {
    const trimmed = value.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setValue("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  }, [value, onSend]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleInput = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value);
    const el = e.target;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 120) + "px";
  };

  return (
    <div className="flex gap-2 px-4 py-2.5 border-t border-border bg-surface shrink-0">
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleInput}
        onKeyDown={handleKeyDown}
        placeholder="Send a prompt..."
        rows={1}
        disabled={disabled}
        className="flex-1 bg-surface-2 border border-border text-text px-3.5 py-2.5 rounded-lg text-sm resize-none min-h-[42px] max-h-[120px] leading-snug focus:outline-none focus:border-accent-dim focus:shadow-[0_0_0_2px_var(--color-accent-glow)] placeholder:text-text-dim disabled:opacity-50"
      />
      <button
        onClick={handleSend}
        disabled={disabled || !value.trim()}
        className="bg-accent-glow-strong border border-accent-dim text-accent px-4 rounded-lg font-semibold text-[13px] whitespace-nowrap transition-all hover:bg-accent hover:text-bg hover:border-accent disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
      >
        Send
      </button>
    </div>
  );
}
