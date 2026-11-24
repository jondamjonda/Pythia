"use client";

// Digest dashboard (LEDGER-M9) — the plain-language compliance digest for one
// business profile. One round-trip to GET /api/digest/{id}/summary (capped
// 50/category) → the four views + the demoted (does_not_bind / context_gaps)
// sections + the disclaimer + the unreviewed-citations banner.
//
// DEFAULT-OFF / BYTE-IDENTICAL: gated behind NEXT_PUBLIC_DIGEST_UI_ENABLED. Off
// → inert placeholder, nav never links here. The server gate digest_enabled
// returns 404 when off → surfaced as the honest off-state, never an error toast.
import { useEffect, useState } from "react";

import { api, type DigestSummaryResponse } from "@/lib/api";
import { ObligationRegister } from "@/components/digest/ObligationRegister";
import { ComplianceChecklist } from "@/components/digest/ComplianceChecklist";
import { GapReport } from "@/components/digest/GapReport";
import { ActionList } from "@/components/digest/ActionList";
import {
  DigestDisclaimer,
  UnreviewedCitationsBanner,
} from "@/components/digest/DigestChrome";

const DIGEST_UI_ENABLED =
  process.env.NEXT_PUBLIC_DIGEST_UI_ENABLED === "1" ||
  process.env.NEXT_PUBLIC_DIGEST_UI_ENABLED === "true";

function isNotFound(message: string): boolean {
  return / → 404:/.test(message) || /\b404\b/.test(message);
}

function OffState() {
  return (
    <main className="mx-auto max-w-3xl px-4 py-16">
      <h1 className="text-2xl font-semibold tracking-tight text-slate-900">
        Přehled zatím není k dispozici
      </h1>
      <p className="mt-2 text-slate-500">
        Tato funkce je momentálně vypnutá, nebo pro tento profil zatím neproběhla
        analýza.
      </p>
    </main>
  );
}

export default function DigestPage({
  params,
}: {
  params: Promise<{ profile_id: string }>;
}) {
  const [profileId, setProfileId] = useState<string | null>(null);
  const [digest, setDigest] = useState<DigestSummaryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [serverOff, setServerOff] = useState(false);

  useEffect(() => {
    params.then((p) => setProfileId(p.profile_id));
  }, [params]);

  useEffect(() => {
    if (!profileId || !DIGEST_UI_ENABLED) return;
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      setError(null);
      try {
        const d = await api.getDigestSummary(profileId, { lang: "cs" });
        if (!cancelled) setDigest(d);
      } catch (e) {
        if (cancelled) return;
        const msg = (e as Error).message;
        // 404 = surface OFF or no analysis run yet → honest off-state.
        if (isNotFound(msg)) setServerOff(true);
        else setError(msg);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [profileId]);

  if (!DIGEST_UI_ENABLED) return <OffState />;
  if (serverOff) return <OffState />;

  if (error) {
    return (
      <main className="mx-auto max-w-3xl px-4 py-16 text-rose-700">
        Přehled se nepodařilo načíst: {error}
      </main>
    );
  }
  if (!digest) {
    return <main className="p-8 text-slate-500">Načítám přehled…</main>;
  }

  return (
    <main className="mx-auto max-w-3xl space-y-10 px-4 py-12">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight text-slate-900">
          Přehled souladu
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Srozumitelný přehled povinností, mezer a doporučených kroků pro vaši
          firmu. Každá položka odkazuje na konkrétní předpis.
        </p>
      </header>

      <UnreviewedCitationsBanner show={!!digest.has_unreviewed_citations} />

      <ObligationRegister
        obligations={digest.obligations}
        doesNotBind={digest.does_not_bind}
      />

      <ComplianceChecklist items={digest.checklist} />

      <GapReport gaps={digest.gaps} contextGaps={digest.context_gaps} />

      <ActionList actions={digest.actions} />

      <DigestDisclaimer text={digest.disclaimer} />
    </main>
  );
}
