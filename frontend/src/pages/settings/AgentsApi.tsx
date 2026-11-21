import { useQuery } from "@tanstack/react-query";
import { configQuery } from "../../lib/config";
import { useIsAdmin } from "../../lib/useIsAdmin";
import { QueryState } from "../../components/QueryState";
import Tokens from "./Tokens";
import { PrivacyCard } from "./PrivacyCard";

/** Settings -> Agents & API (#542): connect an agent, its API tokens, your
 * sign-ins, and (admins) what agents receive by default. A member manages
 * their own tokens and sessions here (GET/DELETE /tokens); the privacy
 * defaults live in admin-only GET/PUT /config, so a member never reads them. */
export default function AgentsApi() {
  const isAdmin = useIsAdmin();
  return (
    <div className="space-y-6">
      <Tokens />
      {isAdmin && <AgentPrivacy />}
    </div>
  );
}

function AgentPrivacy() {
  const config = useQuery(configQuery);
  if (!config.data) return <QueryState query={config} what="the privacy settings" />;
  return <PrivacyCard initial={config.data} />;
}
