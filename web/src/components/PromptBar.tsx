"use client";

import { useState, useRef, useCallback } from "react";

interface Props {
  onSend: (prompt: string) => void;
  disabled?: boolean;
}

/**
 * Prompt bar — the writing surface. Bottom-anchored on mobile with padding
 * matched to iOS home-indicator; free-flow on desktop. `⏎` submits, `⇧⏎`
 * inserts a newline. The "send" affordance is a monospace keyboard hint,
 * not a colored button — the caret going signal-green on focus IS the state.
 */
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
    el.style.height = Math.min(el.scrollHeight, 160) + "px";
  };

  return (
    <div
      className="rule-t bg-bg shrink-0 pb-[env(safe-area-inset-bottom)]"
    >
      <div className="flex items-end gap-3 px-4 md:px-5 py-3">
        {/* Signal glyph — the caret indicator, mirrors the worker's own
            status glyph vocabulary. Goes green on focus-within. */}
        <span
          className="font-mono text-[16px] text-text-dim group-focus-within:text-signal shrink-0 pt-2"
          aria-hidden
        >
          ›
        </span>

        <textarea
          ref={textareaRef}
          value={value}
          onChange={handleInput}
          onKeyDown={handleKeyDown}
          placeholder={disabled ? "worker unavailable" : "send a prompt"}
          rows={1}
          disabled={disabled}
          className="
            flex-1 bg-transparent
            font-sans text-[16px] md:text-[15px] leading-snug text-text
            resize-none min-h-[28px] max-h-[160px] py-1.5
            focus:outline-none placeholder:text-text-dim
            disabled:opacity-50
          "
        />

        <button
          onClick={handleSend}
          disabled={disabled || !value.trim()}
          className="
            font-mono text-[11px] uppercase tracking-wider
            text-text-dim hover:text-signal
            transition-colors cursor-pointer whitespace-nowrap shrink-0 pb-2
            disabled:text-rule disabled:cursor-not-allowed disabled:hover:text-rule
          "
          aria-label="Send prompt"
        >
          send ⏎
        </button>
      </div>
    </div>
  );
}
