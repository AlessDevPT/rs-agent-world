"use client";

/**
 * Sign in and sign up, against Supabase Auth, in the browser.
 *
 * Client-side on purpose: the password is posted straight to Supabase and
 * never reaches an AgentWorld server. `docs/01-ARCHITECTURE.md` says
 * AgentWorld never sees a password, and the only way to mean that is for the
 * credential form not to talk to AgentWorld at all.
 *
 * After a successful sign-in the page is reloaded rather than navigated with
 * the router: the session now lives in a cookie that the *server* has not
 * read yet, and a soft navigation would render the signed-out tree from
 * cache.
 */

import { useState } from "react";

import { createSupabaseBrowserClient } from "@/lib/supabase/browser";

type Mode = "sign-in" | "sign-up";

export function SignInForm({ next }: { readonly next: string }) {
  const [mode, setMode] = useState<Mode>("sign-in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);

    const supabase = createSupabaseBrowserClient();
    const result =
      mode === "sign-in"
        ? await supabase.auth.signInWithPassword({ email, password })
        : await supabase.auth.signUp({
            email,
            password,
            options: { emailRedirectTo: `${window.location.origin}/auth/callback` },
          });

    if (result.error) {
      setError(result.error.message);
      setBusy(false);
      return;
    }

    if (mode === "sign-up" && result.data.session === null) {
      // The Supabase project requires email confirmation, so there is no
      // session yet and nothing to navigate to.
      setNotice("Check your email to confirm the address, then sign in.");
      setBusy(false);
      return;
    }

    window.location.assign(next);
  }

  return (
    <form className="form" onSubmit={submit}>
      <div className="form__row">
        <label className="form__label" htmlFor="email">
          Email
        </label>
        <input
          className="form__input"
          id="email"
          name="email"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </div>

      <div className="form__row">
        <label className="form__label" htmlFor="password">
          Password
        </label>
        <input
          className="form__input"
          id="password"
          name="password"
          type="password"
          autoComplete={mode === "sign-in" ? "current-password" : "new-password"}
          required
          minLength={8}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </div>

      {error !== null && (
        <p className="form__error" role="alert">
          {error}
        </p>
      )}
      {notice !== null && <p className="muted">{notice}</p>}

      <div className="form__actions">
        <button className="button button--primary" type="submit" disabled={busy}>
          {busy ? "Working…" : mode === "sign-in" ? "Sign in" : "Create account"}
        </button>
        <button
          className="button button--quiet"
          type="button"
          onClick={() => {
            setMode(mode === "sign-in" ? "sign-up" : "sign-in");
            setError(null);
            setNotice(null);
          }}
        >
          {mode === "sign-in" ? "Create an account instead" : "I already have an account"}
        </button>
      </div>
    </form>
  );
}
