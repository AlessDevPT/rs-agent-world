import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "AgentWorld",
  description: "Monitor and manage AI agents across providers, languages and applications",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="shell__header">
            <span className="shell__mark">AgentWorld</span>
            <nav className="shell__nav" aria-label="Main">
              {/* Routes arrive in Phase 5; the shell exists now so the
                  layout is not invented later under time pressure. */}
              <span className="shell__nav-item shell__nav-item--active">Overview</span>
              <span className="shell__nav-item">Agents</span>
              <span className="shell__nav-item">Tasks</span>
              <span className="shell__nav-item">Activity</span>
            </nav>
          </header>
          <main className="shell__main">{children}</main>
        </div>
      </body>
    </html>
  );
}
