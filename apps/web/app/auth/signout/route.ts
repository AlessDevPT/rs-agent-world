import { NextResponse, type NextRequest } from "next/server";

import { isAuthConfigured } from "@/lib/supabase/config";
import { createSupabaseServerClient } from "@/lib/supabase/server";

/**
 * Sign out.
 *
 * POST only. A GET would let any page sign a visitor out with an `<img>` tag,
 * which is a nuisance rather than a vulnerability but trivially avoidable.
 */
export async function POST(request: NextRequest) {
  if (isAuthConfigured()) {
    const supabase = await createSupabaseServerClient();
    await supabase.auth.signOut();
  }
  return NextResponse.redirect(new URL("/", new URL(request.url).origin));
}
