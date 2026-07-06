"use client";

import { useState, useEffect, useRef } from "react";
import type { FleetConfig, Machine } from "@/lib/types";
import { fetchConfig, fetchMachines, spawnWorker } from "@/lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  onSpawned: () => void;
}

const NEW_VM_VALUE = "__new_cloud_vm__";

export default function SpawnModal({ open, onClose, onSpawned }: Props) {
  const [config, setConfig] = useState<FleetConfig | null>(null);
  const [machines, setMachines] = useState<Machine[]>([]);
  const [target, setTarget] = useState<string>("");
  const [name, setName] = useState("");
  const [provider, setProvider] = useState("");
  const [vmType, setVmType] = useState("");
  const [model, setModel] = useState("");
  const [instanceType, setInstanceType] = useState("");
  const [region, setRegion] = useState("");
  const [cwd, setCwd] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [copied, setCopied] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    Promise.all([fetchConfig(), fetchMachines()])
      .then(([c, m]) => {
        setConfig(c);
        setProvider(c.provider);
        setMachines(m);
        const firstExternal = m.find(
          (mm) => mm.connected && mm.provider === "external",
        );
        const firstReady = m.find((mm) => mm.connected && mm.status === "ready");
        setTarget(firstExternal?.name || firstReady?.name || NEW_VM_VALUE);
      })
      .catch(() => {});
    setTimeout(() => nameRef.current?.focus(), 100);
  }, [open]);

  if (!open) return null;

  const pickedMachine = machines.find((m) => m.name === target);
  const usingExisting = pickedMachine !== undefined;
  const pCfg = config?.providers[provider];

  const handleSubmit = async () => {
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    if (!usingExisting) {
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const req: Record<string, string> = {
        name: name.trim(),
        machine_name: pickedMachine!.name,
      };
      if (model.trim()) req.model = model.trim();
      if (pickedMachine!.provider !== "devcontainer") {
        req.cwd = cwd.trim() || "~";
      }
      await spawnWorker(req as never);
      onSpawned();
      onClose();
      setName("");
      setModel("");
      setCwd("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to spawn");
    } finally {
      setSubmitting(false);
    }
  };

  const cloudCommand = (() => {
    const parts = ["cfleet", "machine", "create", name.trim() || "<name>"];
    if (provider) parts.push("--provider", provider);
    if (vmType) parts.push("--type", vmType);
    if (region.trim()) parts.push("--region", region.trim());
    if (instanceType.trim()) parts.push("--instance-type", instanceType.trim());
    return parts.join(" ");
  })();

  const machineSummary = (m: Machine): string => {
    const parts: string[] = [m.name, m.provider || "unknown"];
    parts.push(m.connected ? m.status : "offline");
    const workerCount = m.worker_names?.length || 0;
    if (workerCount > 0) parts.push(`${workerCount} worker${workerCount === 1 ? "" : "s"}`);
    return parts.join(" · ");
  };

  return (
    <div
      className="fixed inset-0 z-50 flex justify-center items-stretch sm:items-center sm:p-5"
      style={{ backgroundColor: "rgba(26, 26, 26, 0.35)" }}
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className="bg-bg w-full sm:max-w-[460px] flex flex-col animate-[fadeIn_0.2s_ease]
                   h-[100dvh] sm:h-auto sm:max-h-[calc(100dvh-2.5rem)] sm:border sm:border-rule"
      >
        <div className="flex items-center justify-between px-5 py-4 rule-b shrink-0">
          <div>
            <div className="font-eyebrow">New worker</div>
            <div className="font-callsign text-[18px] text-text mt-1 leading-tight">Spawn</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="font-mono text-[11px] uppercase tracking-wider text-text-dim hover:text-text cursor-pointer"
            aria-label="Close"
          >
            close
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 pt-4 pb-2">

        <Field label="Target">
          <select
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            className="w-full bg-panel text-text px-2.5 py-2 text-[14px] focus:outline-none focus:border-signal font-mono border border-rule"
          >
            {machines.map((m) => (
              <option
                key={m.name}
                value={m.name}
                disabled={!m.connected}
              >
                {machineSummary(m)}
              </option>
            ))}
            <option value={NEW_VM_VALUE}>+ New cloud VM...</option>
          </select>
          {pickedMachine && (
            <div className="font-mono text-[11px] text-text-dim mt-1.5">
              {pickedMachine.hostname || pickedMachine.ip || ""}
              {pickedMachine.os_info ? ` · ${pickedMachine.os_info}` : ""}
            </div>
          )}
        </Field>

        <Field label="Name">
          <input
            ref={nameRef}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-worker"
            className="w-full bg-panel text-text px-2.5 py-2 text-[14px] focus:outline-none focus:border-signal font-mono border border-rule"
            onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
          />
        </Field>

        <Field label="Model">
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={config?.model || "claude-opus-4-6"}
            className="w-full bg-panel text-text px-2.5 py-2 text-[14px] focus:outline-none focus:border-signal font-mono border border-rule"
          />
        </Field>

        {usingExisting && pickedMachine?.provider === "external" && (
          <Field label="Working dir on the machine">
            <input
              value={cwd}
              onChange={(e) => setCwd(e.target.value)}
              placeholder="~"
              className="w-full bg-panel text-text px-2.5 py-2 text-[14px] focus:outline-none focus:border-signal font-mono border border-rule"
            />
          </Field>
        )}

        {!usingExisting && (
          <>
            <Field label="Provider">
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                className="w-full bg-panel text-text px-2.5 py-2 text-[14px] focus:outline-none focus:border-signal font-mono border border-rule"
              >
                {config ? (
                  Object.keys(config.providers).map((p) => (
                    <option key={p} value={p}>
                      {p}
                      {p === config.provider ? " (default)" : ""}
                    </option>
                  ))
                ) : (
                  <option>Loading...</option>
                )}
              </select>
              {pCfg && (
                <div className="font-mono text-[11px] text-text-dim mt-1.5">
                  {provider === "devcontainer"
                    ? "Local Docker container"
                    : `${provider} (${pCfg.region})`}
                </div>
              )}
            </Field>

            <Field label="VM Type">
              <select
                value={vmType}
                onChange={(e) => setVmType(e.target.value)}
                className="w-full bg-panel text-text px-2.5 py-2 text-[14px] focus:outline-none focus:border-signal font-mono border border-rule"
              >
                <option value="">regular</option>
                <option value="snp">snp (AMD SEV-SNP)</option>
                <option value="tdx">tdx (Intel TDX)</option>
              </select>
            </Field>

            <Field label="Instance Type">
              <input
                value={instanceType}
                onChange={(e) => setInstanceType(e.target.value)}
                placeholder={pCfg?.instance_type || "auto"}
                className="w-full bg-panel text-text px-2.5 py-2 text-[14px] focus:outline-none focus:border-signal font-mono border border-rule"
              />
            </Field>

            <Field label="Region">
              <input
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                placeholder={pCfg?.region || ""}
                className="w-full bg-panel text-text px-2.5 py-2 text-[14px] focus:outline-none focus:border-signal font-mono border border-rule"
              />
            </Field>

            <div className="mt-5 mb-1">
              <div className="font-eyebrow mb-1.5">
                Run this on your laptop
              </div>
              <div className="relative">
                <pre className="bg-panel p-3 pr-16 font-mono text-[12px] text-text overflow-x-auto whitespace-pre-wrap break-all border border-rule">
                  {cloudCommand}
                </pre>
                <button
                  type="button"
                  onClick={async (ev) => {
                    ev.preventDefault();
                    try {
                      await navigator.clipboard.writeText(cloudCommand);
                      setCopied(true);
                      setTimeout(() => setCopied(false), 1200);
                    } catch {
                      /* ignore */
                    }
                  }}
                  className="absolute top-2 right-2 font-mono text-[10px] uppercase tracking-wider px-1.5 py-0.5 text-text-dim hover:text-signal bg-bg cursor-pointer transition-colors"
                >
                  {copied ? "copied" : "copy"}
                </button>
              </div>
              <div className="font-mono text-[11px] text-text-dim mt-2 leading-relaxed">
                Cloud VMs are provisioned from your laptop (uses your gcloud/az login).
                After it finishes, the machine self-registers — come back and pick it
                from Target.
              </div>
            </div>
          </>
        )}

        {error && (
          <div className="font-mono text-[12px] text-text mt-3 pl-3 rule-l" style={{ borderLeftColor: "var(--color-signal)" }}>
            {error}
          </div>
        )}

        </div>

        <div className="flex gap-5 justify-end items-center px-5 py-4 rule-t shrink-0 bg-bg">
          <button
            onClick={onClose}
            className="font-mono text-[11px] uppercase tracking-wider text-text-dim hover:text-text cursor-pointer"
          >
            cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting || !usingExisting}
            className="font-mono text-[11px] uppercase tracking-wider px-4 py-2 bg-text text-bg hover:bg-signal transition-colors cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-text"
          >
            {submitting ? "spawning···" : "spawn ⏎"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-4">
      <label className="font-eyebrow block mb-1.5">
        {label}
      </label>
      {children}
    </div>
  );
}
