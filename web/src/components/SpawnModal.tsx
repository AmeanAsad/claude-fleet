"use client";

import { useState, useEffect, useRef } from "react";
import type { FleetConfig } from "@/lib/types";
import { fetchConfig, spawnWorker } from "@/lib/api";

interface Props {
  open: boolean;
  onClose: () => void;
  onSpawned: () => void;
}

export default function SpawnModal({ open, onClose, onSpawned }: Props) {
  const [config, setConfig] = useState<FleetConfig | null>(null);
  const [name, setName] = useState("");
  const [provider, setProvider] = useState("");
  const [vmType, setVmType] = useState("");
  const [model, setModel] = useState("");
  const [instanceType, setInstanceType] = useState("");
  const [region, setRegion] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      fetchConfig()
        .then((c) => {
          setConfig(c);
          setProvider(c.provider);
        })
        .catch(() => {});
      setTimeout(() => nameRef.current?.focus(), 100);
    }
  }, [open]);

  if (!open) return null;

  const pCfg = config?.providers[provider];

  const handleSubmit = async () => {
    if (!name.trim()) {
      setError("Name is required");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      const req: Record<string, string> = { name: name.trim() };
      if (provider) req.provider = provider;
      if (vmType) req.vm_type = vmType;
      if (model.trim()) req.model = model.trim();
      if (instanceType.trim()) req.instance_type = instanceType.trim();
      if (region.trim()) req.region = region.trim();
      await spawnWorker(req as never);
      onSpawned();
      onClose();
      setName("");
      setModel("");
      setInstanceType("");
      setRegion("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "Failed to spawn");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-5"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="bg-surface border border-border rounded-xl p-5 w-full max-w-[400px] animate-[fadeIn_0.2s_ease]">
        <h2 className="text-[15px] font-semibold text-text mb-4">
          Spawn Worker
        </h2>

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

        <Field label="Model">
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder={config?.model || "claude-opus-4-6"}
            className="w-full bg-surface-2 border border-border text-text px-2.5 py-2 rounded-md text-[13px] focus:outline-none focus:border-accent-dim"
          />
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

        {error && (
          <div className="text-red text-xs mt-2">{error}</div>
        )}

        <div className="flex gap-2 justify-end mt-4">
          <button
            onClick={onClose}
            className="px-3.5 py-1.5 rounded-md text-[13px] border border-border text-text-dim hover:border-text hover:text-text transition-all cursor-pointer"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting}
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
