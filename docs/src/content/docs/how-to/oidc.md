---
title: Single sign-on with OIDC
description: Wire Purser to a generic OpenID provider (Authentik as the example) with three runtime-editable settings that can never lock you out.
---

Purser can authenticate admins against any generic OpenID Connect provider.
Authentik is the worked example below, but the three settings are standard OIDC
and apply to any compliant provider.

## Register Purser with your provider

Create an OAuth2/OpenID application in your provider (a confidential client,
authorization code flow) and set its redirect URI to, exactly, your
`PURSER_PUBLIC_URL` followed by `/auth/oidc/callback`.

The redirect base is your instance's [`PURSER_PUBLIC_URL`](../../reference/configuration/)
— which is environment-only, since it also drives cookie security. If
`PURSER_PUBLIC_URL` is `https://purser.example.com`, register
`https://purser.example.com/auth/oidc/callback`. Note the client id and client
secret your provider issues.

## The three settings

Set the issuer, client id, and client secret. SSO **enables only when all three
are present**:

| Setting | Value |
| --- | --- |
| `PURSER_OIDC_ISSUER` | Your provider's issuer URL (e.g. an Authentik application's OpenID configuration base). |
| `PURSER_OIDC_CLIENT_ID` | The client id from the provider. |
| `PURSER_OIDC_CLIENT_SECRET` | The client secret (write-only: encrypted at rest, never echoed back, masked to set/unset in audit rows). |

These are **runtime-editable** in **Config -> Settings** (or the admin-scoped
`/config` API); the environment values are just the bootstrap default, and a
database override wins. Every change is audited.

## Who gets in

An SSO identity is the provider's issuer together with the subject (`sub`) it
issues: the same subject at another provider is another person. An identity
already linked to a Purser user signs straight in. Otherwise Purser looks for
an account with the id_token's email, ignoring letter case, and only when the
provider asserts `email_verified` in the id_token. An unverified email is
refused: a provider that issues tokens with subject-chosen emails must not be
able to take over a local account.

| The account with that email | What happens |
| --- | --- |
| Is not linked to SSO | It links to this identity on first sign-in (audited `user.link_oidc`). |
| Is linked through another provider | It relinks to this identity (audited `user.link_oidc`, with the previous issuer). This is how you move to a new provider. |
| Is linked to another subject of this provider | Refused with `403` "this account is linked to a different SSO identity; ask an administrator to unlink it", and audited `user.link_oidc_refused`. Nothing changes. |
| Does not exist | Refused with "account not provisioned" by default. Pre-create the user in the admin UI (matching the verified email), or set `PURSER_OIDC_AUTO_PROVISION` (also runtime-editable) to auto-create member accounts, again only for verified emails. |

`email_verified` must be the JSON boolean `true`; a string `"true"` counts as
unverified. An account stays linked to its identity: a later change of email
at the provider does not move it. So only connect a provider that verifies the
emails it marks as verified.

Emails match regardless of letter case everywhere: SSO linking,
auto-provisioning, password sign-in and the rule that every account has its
own email. Purser keeps each email as it was entered.

## Unlink an SSO identity

When someone's identity at your provider changes (a recreated account, a new
subject), Purser refuses it until an admin unlinks the old one. On the
**Users** page, choose **Unlink SSO** on that user's row; over the API it is
`DELETE /users/{user_id}/oidc` with an admin token. The account, its password
if it has one, and its history stay; the next SSO sign-in that asserts the
account's verified email links it again. The unlink is audited
`user.unlink_oidc` with the admin as actor. Unlinking a user who has no SSO
link answers `409`.

Links made before Purser recorded the provider were attributed to the provider
configured at upgrade time. If none was configured then, such a link counts as
belonging to whichever provider is configured now: its own subject signs in
and records the provider, and any other subject is refused until you unlink it.

## Move to another provider

1. Make sure the new provider asserts verified emails that match your Purser
   accounts (letter case does not matter).
2. Change the issuer, client id and client secret in **Config -> Settings**.
3. Each person signs in through the new provider once. Their account relinks
   to the new identity, audited with the previous issuer. Accounts nobody signs
   in with stay linked to the old provider until they do, or until an admin
   unlinks them.

Moving back to a provider you used before works the same way.

## What Purser checks

Each sign-in is an authorization code flow with three per-attempt secrets,
kept in a short-lived cookie (HttpOnly, SameSite=Lax, ten minutes). When
`PURSER_PUBLIC_URL` is `https://`, it is `__Host-purser_oidc_state` with
`Secure` and `Path=/`: browsers accept that prefix only from this exact host,
so a sibling subdomain cannot plant a sign-in attempt of its own. On plain
HTTP, where the prefix is unavailable, it is `purser_oidc_state`, scoped to
`/auth/oidc`.

- **state** ties the provider's redirect back to the browser that started the
  sign-in, so a link to someone else's callback is refused.
- **nonce** must come back unchanged inside the id_token.
- **PKCE** (`S256`): the code exchange carries the verifier, so a code seen in
  transit is useless on its own.

The provider's discovery document (`/.well-known/openid-configuration` under
the issuer) must name the issuer you configured; a trailing slash is the only
difference allowed. When the issuer is `https://`, its authorization, token
and JWKS endpoints must be `https://` too. Discovery and the signing keys are
fetched for every sign-in, so a key rotation at the provider takes effect at
once. Every request to the provider has a 10-second timeout and does not follow
redirects.

The id_token from the token endpoint must then pass:

| Check | Rule |
| --- | --- |
| Signature | `RS256` or `ES256`, by a key from the provider's JWKS selected by `kid` (a token without a `kid` is accepted only when the JWKS has one key). `none`, the HMAC algorithms and any other algorithm are refused, as is a key published for encryption or of the wrong type. |
| `iss` | Equal to the discovery document's issuer. |
| `aud` / `azp` | `aud` contains the client id. A token for several audiences needs `azp` equal to the client id; any `azp` must be the client id. |
| `exp` | Required; the token is refused once it has expired, with no grace period. |
| `iat`, `nbf` | `iat` is required. Up to 60 seconds of clock difference with the provider is tolerated for a token stamped slightly in the future. |
| `sub`, `nonce` | `sub` is a non-empty string; `nonce` matches this sign-in. |
| `email` | Required, a string. |

Unknown header parameters are ignored unless the token lists them in `crit`.

## When sign-in fails

The callback answers with a fixed message and logs the failing check, never a
code or token:

| Status | Meaning |
| --- | --- |
| `400` | The state did not match (an expired, missing or foreign sign-in attempt), the callback had no code, or the provider reported an error such as a cancelled sign-in. Start again from the login page. |
| `401` | The id_token failed one of the checks above. The log line `oidc.id_token_rejected` names which. |
| `403` | The token was valid but Purser refused the account (see "Who gets in"). |
| `502` | The provider could not be reached, or answered something unusable: discovery, the JWKS or the token exchange failed (log line `oidc.provider_error`, with the step). A wrong client secret shows up here. |

## It can never lock you out

OIDC is safe to edit live. The OIDC client is rebuilt per request and
**password login is always available as an independent fallback**, so a wrong
issuer or a bad secret cannot lock the admin out — you log in with a password and
fix the value. An explicit **null** override on the issuer or client id is the
kill switch: it disables SSO even when the environment configures it.

## Try it

With all three set, the login page offers an SSO button that redirects to your
provider and back through the callback URI above. If the round trip fails, log in
with a password, open **Config -> Settings**, and re-check the issuer URL and the
redirect URI registered at the provider — those two are the usual culprits.

The authoritative spec is
[`DESIGN.md`](https://github.com/mlamoure/purser/blob/main/DESIGN.md)
section 9 (security model); every setting is listed in
[Configuration](../../reference/configuration/).
