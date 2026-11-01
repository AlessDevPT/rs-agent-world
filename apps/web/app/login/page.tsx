import Link from "next/link";
import { redirect } from "next/navigation";

import { safeInternalPath } from "@/lib/redirects";
import { isAuthConfigured } from "@/lib/supabase/config";
import { getAccessToken } from "@/lib/supabase/server";

import { SignInForm } from "./sign-in-form";

export const dynamic = "force-dynamic";

export const metadata = { title: "Sign in · AgentWorld" };

/**
 * The sign-in page.
 *
 * `next` carries where the visitor was headed before being sent here, so
 * signing in returns them to it. It is validated as a same-site path before
 * use: an unchecked redirect target taken from a query string is an open
 * redirect, and a sign-in page is exactly where one is most useful to an
 * attacker.
 */
export default async function LoginPage({
  searchParams,
}: {
  readonly searchParams: Promise<{ next?: string }>;
}) {
  if (!isAuthConfigured()) redirect("/");
  if ((await getAccessToken()) !== null) redirect("/");

  const { next } = await searchParams;
  const target = safeInternalPath(next);

  return (
    <section className="stack stack--narrow">
      <div>
        <h1 className="title">Sign in</h1>
        <p className="muted">
          Accounts live in Supabase Auth. AgentWorld never sees your password - the form
          below talks to Supabase directly.
        </p>
      </div>

      <div className="card">
        <SignInForm next={target} />
      </div>

      <p className="muted">
        <Link href="/">Back to the start</Link>
      </p>
    </section>
  );
}
