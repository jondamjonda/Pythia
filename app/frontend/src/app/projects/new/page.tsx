"use client";

// Intake form — plan §13.5 Day 14. POST /api/projects, then push to the
// findings page. Field list mirrors `analysis/l0_intake.ProjectIntake`.

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api, type ProjectIntakePayload } from "@/lib/api";

const SECTOR_OPTIONS = [
  "fin_services",
  "investment_funds",
  "banking",
  "insurance",
  "real_estate",
  "tech",
  "healthcare",
  "energy",
  "manufacturing",
  "transport",
  "retail",
  "crypto",
];

const ENTITY_OPTIONS = [
  "AIF",
  "AIFM",
  "UCITS",
  "investment_company",
  "investment_firm",
  "bank",
  "credit_institution",
  "payment_institution",
  "e_money",
  "fintech",
  "crypto_provider",
  "casp",
  "insurance_undertaking",
  "broker",
];

export default function NewProjectPage() {
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("Test AIF marketing in CZ");
  const [sector, setSector] = useState("fin_services");
  const [jurisdiction, setJurisdiction] = useState("EU,CZ");
  const [entityType, setEntityType] = useState("AIF");
  const [declaredIntent, setDeclaredIntent] = useState(
    "EU AIFM marketing an EU alternative investment fund to professional investors in the Czech Republic. Need to understand pre-marketing rules, notification deadlines to ČNB, local depositary obligations.",
  );
  const [declaredRisks, setDeclaredRisks] = useState("passporting_friction, local_depositary_cost");

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    const body: ProjectIntakePayload = {
      name,
      sector,
      jurisdiction_scope: jurisdiction.split(",").map((s) => s.trim()).filter(Boolean),
      entity_type: entityType || undefined,
      declared_intent: declaredIntent || undefined,
      declared_risks: declaredRisks
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    };
    try {
      const project = await api.createProject(body);
      router.push(`/projects/${project.id}`);
    } catch (err) {
      setError((err as Error).message);
      setSubmitting(false);
    }
  }

  return (
    <main className="mx-auto max-w-2xl px-4 py-10">
      <h1 className="mb-1 text-2xl font-semibold tracking-tight">New project</h1>
      <p className="mb-6 text-sm text-slate-500">
        Describe the project; the analysis cascade will pick the applicable rules and extract obligations.
      </p>

      {error && (
        <div className="mb-4 rounded border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </div>
      )}

      <form onSubmit={onSubmit} className="space-y-4">
        <Field label="Project name">
          <input
            className="w-full rounded border px-3 py-2"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Sector">
            <select
              className="w-full rounded border px-3 py-2"
              value={sector}
              onChange={(e) => setSector(e.target.value)}
            >
              {SECTOR_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Entity type">
            <select
              className="w-full rounded border px-3 py-2"
              value={entityType}
              onChange={(e) => setEntityType(e.target.value)}
            >
              <option value="">(unspecified)</option>
              {ENTITY_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <Field label="Jurisdiction (comma-separated)" hint="e.g. EU,CZ">
          <input
            className="w-full rounded border px-3 py-2"
            value={jurisdiction}
            onChange={(e) => setJurisdiction(e.target.value)}
            required
          />
        </Field>

        <Field
          label="Declared intent"
          hint="Plain-English description of what the project is trying to do."
        >
          <textarea
            className="h-28 w-full rounded border px-3 py-2"
            value={declaredIntent}
            onChange={(e) => setDeclaredIntent(e.target.value)}
          />
        </Field>

        <Field label="Declared risks (comma-separated)" hint="optional">
          <input
            className="w-full rounded border px-3 py-2"
            value={declaredRisks}
            onChange={(e) => setDeclaredRisks(e.target.value)}
          />
        </Field>

        <button
          type="submit"
          disabled={submitting}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {submitting ? "Creating…" : "Create project →"}
        </button>
      </form>
    </main>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-slate-700">{label}</span>
      {hint && <span className="mb-1 block text-xs text-slate-400">{hint}</span>}
      {children}
    </label>
  );
}
