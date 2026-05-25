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
    idle: "text-green",
    working: "text-yellow",
    spawning: "text-cyan",
    provisioning: "text-cyan",
    creating: "text-cyan",
    errored: "text-red",
    stopped: "text-text-dim",
    ready: "text-green",
  };
  return map[status] || "text-text";
}

export function dotColor(status: string): string {
  const map: Record<string, string> = {
    idle: "bg-green",
    working: "bg-yellow animate-pulse",
    spawning: "bg-cyan animate-pulse",
    provisioning: "bg-cyan animate-pulse",
    errored: "bg-red",
    stopped: "bg-text-dim",
  };
  return map[status] || "bg-text-dim";
}
