import { useIsAdmin } from "../../lib/useIsAdmin";
import Reachability from "./Reachability";
import Tokens from "./Tokens";

/** Settings -> Agents & API (#542): connect an agent, its API tokens and your
 * sign-ins. A member manages their own tokens and sessions here (GET/DELETE
 * /tokens). Privacy with agents is a per-call choice the agent makes
 * (`withhold`, #613), not a setting; the audit log shows when it was used.
 * An admin also sees how Purser is reached (#632), next to the connection
 * steps a new token comes with. */
export default function AgentsApi() {
  const isAdmin = useIsAdmin();
  return (
    <div className="space-y-6">
      {isAdmin && <Reachability />}
      <Tokens />
    </div>
  );
}
