import type { ReactNode } from "react";
import { ControlChrome } from "@/components/control/ControlChrome";

// Wraps the actual control panels in the rail + status-bar chrome. The /control/unlock screen sits
// outside this group, so it renders without the chrome.
export default function PanelsLayout({ children }: { children: ReactNode }) {
  return <ControlChrome>{children}</ControlChrome>;
}
