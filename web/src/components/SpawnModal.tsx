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
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    Promise.all([fetchConfig(), fetchMachines()])
      .then(([c, m]) => {
        setConfig(c);
        setProvider(c.provider);
        setMachines(m);
        // Default target: first connected external machine, else "new cloud VM"
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
      // Cloud provisioning runs from the user's laptop (uses their gcloud/az
      // credentials), so we don't submit anything — the modal instead shows
      // the CLI command to paste.
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

  const [copied, setCopied] = useState(false);

  const machineSummary = (m: Machine): string => {
    const parts: string[] = [m.name, m.provider || "unknown"];
    parts.push(m.connected ? m.status : "offline");
    const workerCount = m.worker_names?.length || 0;
    if (workerCount > 0) parts.push(`${workerCount} worker${workerCount === 1 ? "" : "s"}`);
    return parts.join(" · ");
  };

  return (
    <div
      className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-5"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="bg-surface border border-border rounded-xl p-5 w-full max-w-[440px] animate-[fadeIn_0.2s_ease]">
        <h2 className="text-[15px] font-semibold text-text mb-4">Spawn Worker</h2>

        <Field label="Target">
          <select
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            className="w-full bg-surface-2 border border-border text-text px-2.5 py-2 rounded-md text-[13px] focus:outline-none focus:border-accent-dim"
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
            <option value={NEW_VM_VALUE}>+ New cloud VM…</option>
          </select>
          {pickedMachine && (
            <div className="text-[10px] text-text-dim mt-0.5 italic">
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
            className="w-full bg-surface-2 border border-border text-text px-2.5 py-2 rounded-md text-[13px] focus:outline-none focus:border-accent-dim"
            onKeyDown={(e) => e.key === "Enter" && handleSubmit()}
          />
        </Field>

        <Field label="Model">
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={config?.model || "claude-opus-4-6"}
            className="w-full bg-surface-2 border border-border text-text px-2.5 py-2 rounded-md text-[13px] focus:outline-none focus:border-accent-dim"
          />
        </Field>

        {usingExisting && pickedMachine?.provider === "external" && (
          <Field label="Working dir on the machine">
            <input
              value={cwd}
              onChange={(e) => setCwd(e.target.value)}
              placeholder="~"
              className="w-full bg-surface-2 border border-border text-text px-2.5 py-2 rounded-md text-[13px] focus:outline-none focus:border-accent-dim"
            />
          </Field>
        )}

        {!usingExisting && (
          <>
            <Field label="Provider">
              <select
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                className="w-full bg-surface-2 border border-border text-text px-2.5 py-2 rounded-md text-[13px] focus:outline-none focus:border-accent-dim"
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
                <div className="text-[10px] text-text-dim mt-0.5 italic">
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
                className="w-full bg-surface-2 border border-border text-text px-2.5 py-2 rounded-md text-[13px] focus:outline-none focus:border-accent-dim"
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
                className="w-full bg-surface-2 border border-border text-text px-2.5 py-2 rounded-md text-[13px] focus:outline-none focus:border-accent-dim"
              />
            </Field>

            <Field label="Region">
              <input
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                placeholder={pCfg?.region || ""}
                className="w-full bg-surface-2 border border-border text-text px-2.5 py-2 rounded-md text-[13px] focus:outline-none focus:border-accent-dim"
              />
            </Field>

            <div className="mt-3 mb-1">
              <div className="text-[11px] text-text-dim mb-1 uppercase tracking-wide">
                Run this on your machine
              </div>
              <div className="relative">
                <pre className="bg-surface-2 border border-border rounded-md p-2.5 pr-14 text-[12px] font-mono text-text overflow-x-auto whitespace-pre-wrap break-all">
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
                  className="absolute top-1.5 right-1.5 text-[10px] px-1.5 py-0.5 rounded border border-border text-text-dim hover:border-text hover:text-text bg-surface/80 cursor-pointer transition-colors"
                >
                  {copied ? "copied" : "copy"}
                </button>
              </div>
              <div className="text-[10px] text-text-dim mt-1 italic">
                Cloud VMs are provisioned from your laptop (uses your gcloud/az login).
                After it finishes, the machine self-registers — come back to this dialog
                and pick it from the Target list.
              </div>
            </div>
          </>
        )}

        {error && <div className="text-red text-xs mt-2">{error}</div>}

        <div className="flex gap-2 justify-end mt-4">
          <button
            onClick={onClose}
            className="px-3.5 py-1.5 rounded-md text-[13px] border border-border text-text-dim hover:border-text hover:text-text transition-all cursor-pointer"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting || !usingExisting}
            className="px-3.5 py-1.5 rounded-md text-[13px] font-semibold border border-accent-dim text-accent bg-accent-glow-strong hover:bg-accent hover:text-bg hover:border-accent transition-all disabled:opacity-50 cursor-pointer"
          >
            {submitting ? "Spawning..." : "Spawn"}
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
    <div className="mb-3">
      <label className="block text-[11px] text-text-dim mb-1 uppercase tracking-wide">
        {label}
      </label>
      {children}
    </div>
  );
}
