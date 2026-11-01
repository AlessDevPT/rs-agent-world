import { redirect } from "next/navigation";

import { requireSession } from "@/lib/session";

import { CreateWorkspaceForm } from "./create-workspace-form";

export const dynamic = "force-dynamic";

export const metadata = { title: "Create a workspace · AgentWorld" };

/**
 * First-run: a signed-in user who belongs to no workspace.
 *
 * Not an error page. `GET /v1/me` reports `needs_onboarding`, and this is
 * what that means on screen.
 */
export default async function OnboardingPage() {
  const { me } = await requireSession("/onboarding");
  // Someone who already has a workspace does not need this screen, and
  // letting them sit on it would make "create another" the default action.
  if (!me.needs_onboarding) redirect("/");

  return (
    <section className="stack stack--narrow">
      <div>
        <h1 className="title">Create your first workspace</h1>
        <p className="muted">
          A workspace holds projects, members and API keys. You will be its owner.
          Signed in as {me.user.email}.
        </p>
      </div>

      <div className="card">
        <CreateWorkspaceForm />
      </div>
    </section>
  );
}
