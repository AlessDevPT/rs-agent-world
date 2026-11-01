import type { Metadata } from "next";
import Link from "next/link";

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
            <Link className="shell__mark" href="/">
              AgentWorld
            </Link>
            {/* The dashboard nav - agents, tasks, activity - belongs to a
                workspace, so it lives in the workspace layout rather than
                here, where /login would also render it. */}
          </header>
          <main className="shell__main">{children}</main>
        </div>
      </body>
    </html>
  );
}
