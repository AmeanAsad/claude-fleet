export function fmtNum(n: number): string {
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

export function esc(s: string): string {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

export function renderMarkdown(text: string): string {
  let html = esc(text);
  html = html.replace(
    /```(\w*)\n([\s\S]*?)```/g,
    (_, _lang, code) => `<pre><code>${code}</code></pre>`,
  );
  html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/(?<!\w)\*([^*]+)\*(?!\w)/g, "<em>$1</em>");
  return html;
}

export function statusColor(status: string): string {
  const map: Record<string, string> = {
    idle: "text-text-mid",
    working: "text-signal",
    spawning: "text-text-dim",
    provisioning: "text-text-dim",
    creating: "text-text-dim",
    errored: "text-text",
    stopped: "text-text-dim",
    ready: "text-text-mid",
  };
  return map[status] || "text-text-mid";
}

/** Status glyph — a single monospace character that carries the state.
 *  This is the signature element: color is spent only on `working`, everything
 *  else differentiates by glyph shape and text weight. Also used as the
 *  favicon-updater so the browser tab reads e.g. `▪▪▫· cfleet` at a glance.
 */
export function statusGlyph(worker: {
  status: string;
  connected?: boolean;
}): { glyph: string; animate: boolean; className: string } {
  if (worker.connected === false) {
    return { glyph: "·", animate: false, className: "text-text-dim" };
  }
  switch (worker.status) {
    case "working":
      return { glyph: "▪", animate: true, className: "text-signal" };
    case "spawning":
    case "provisioning":
    case "creating":
      return { glyph: "▫", animate: true, className: "text-text-mid" };
    case "errored":
      return { glyph: "×", animate: false, className: "text-text" };
    case "stopped":
      return { glyph: "·", animate: false, className: "text-text-dim" };
    case "idle":
    case "ready":
    default:
      return { glyph: "▪", animate: false, className: "text-text" };
  }
}

/** Format a callsign the way ATC would say it: uppercase, dot-separated. */
export function callsign(name: string, machine?: string): string {
  const parts = [name];
  if (machine) parts.push(machine);
  return parts.join("·").toUpperCase();
}

/** Compact relative time — "3m", "2h", "yesterday". Tabular-nums friendly. */
export function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Date.now() - then;
  const s = Math.floor(diff / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d`;
  return new Date(iso).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

/** Hanging-timestamp gutter format: "HH:MM" in tabular monospace.
 *  Displayed to the left of each message so scanning down the left edge
 *  gives you the tempo of the conversation without reading it.
 */
export function hangTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** Kept for legacy callers still using dotColor — now the same as the glyph
 *  color, so nothing breaks visually while components are being rewritten. */
export function dotColor(status: string): string {
  const map: Record<string, string> = {
    idle: "bg-text",
    working: "bg-signal animate-signal",
    spawning: "bg-text-dim animate-signal",
    provisioning: "bg-text-dim animate-signal",
    errored: "bg-text",
    stopped: "bg-text-dim",
  };
  return map[status] || "bg-text-dim";
}
