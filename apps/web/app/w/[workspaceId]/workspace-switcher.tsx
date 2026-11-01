"use client";

import { useRouter } from "next/navigation";

import type { MemberRole, Workspace, WorkspaceMembership } from "@/lib/api/tenancy";

/**
 * Switch workspaces, or start a new one.
 *
 * A `<select>` rather than a custom dropdown: it is keyboard-accessible and
 * usable on a phone without any work, and a bespoke menu would be the first
 * thing to re-implement badly.
 */
export function WorkspaceSwitcher({
  current,
  memberships,
  role,
}: {
  readonly current: Workspace;
  readonly memberships: readonly WorkspaceMembership[];
  readonly role: MemberRole;
}) {
  const router = useRouter();

  return (
    <div className="switcher">
      <label className="switcher__label" htmlFor="workspace">
        Workspace
      </label>
      <select
        className="switcher__select"
        id="workspace"
        value={current.id}
        onChange={(event) => {
          const next = event.target.value;
          if (next === "__new__") {
            router.push("/onboarding");
            return;
          }
          router.push(`/w/${next}`);
        }}
      >
        {memberships.map(({ workspace }) => (
          <option key={workspace.id} value={workspace.id}>
            {workspace.name}
          </option>
        ))}
        <option value="__new__">+ New workspace…</option>
      </select>
      <span className="pill pill--role" title={`Your role in ${current.name}`}>
        {role}
      </span>
    </div>
  );
}
