/**
 * Copyright (c) 2026-present Mesh contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 */

type Props = {
  email: string | null | undefined;
  isAgent: boolean;
  className?: string;
};

export function MemberEmail({ email, isAgent, className = "" }: Props) {
  const isGenerated = email && isAgent && /(?:@agentpm\.local$|^bot_user_[^@]+@plane\.(?:so|local)$)/i.test(email);
  const contactEmail = isGenerated ? undefined : email ?? undefined;

  return (
    <span className={`block w-48 max-w-full truncate ${className}`} title={contactEmail}>
      {contactEmail || "—"}
    </span>
  );
}
