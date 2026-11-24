import type { ReactNode } from "react";
import type { Metadata } from "next";
import { ControlProviders } from "@/components/control/Providers";

export const metadata: Metadata = {
  title: "Polarius — Control Room",
};

// Outer control layout: the dark shell + the TanStack Query provider for every /control/* route
// (including the chrome-less unlock screen). The chrome (rail + status bar) is added by the nested
// (panels) layout, so the lock screen renders bare.
export default function ControlLayout({ children }: { children: ReactNode }) {
  return (
    <div className="control-shell">
      <ControlProviders>{children}</ControlProviders>
    </div>
  );
}
