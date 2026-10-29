import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import type { components } from "../api/schema";
import { Badge, Button, Card, CardTitle, ErrorText, Input, Label, Table, Td, Th } from "../components/ui";

type UserOut = components["schemas"]["UserOut"];

export default function Users() {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("member");
  const [error, setError] = useState<string | null>(null);

  const users = useQuery({
    queryKey: ["users"],
    queryFn: async () => (await api.GET("/users")).data ?? [],
  });
  const create = useMutation({
    mutationFn: async () => {
      const { error: err } = await api.POST("/users", { body: { email, password, role } });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => {
      setEmail("");
      setPassword("");
      qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: (e) => setError(e.message),
  });
  // Admin-only (the page is): frees the account for another SSO identity at
  // the same provider, which sign-in otherwise refuses (#429).
  const [unlinkError, setUnlinkError] = useState<string | null>(null);
  const unlink = useMutation({
    mutationFn: async (u: UserOut) => {
      const { error: err } = await api.DELETE("/users/{user_id}/oidc", {
        params: { path: { user_id: u.id } },
      });
      if (err) throw new Error(errorDetail(err));
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
    onError: (e) => setUnlinkError(e.message),
  });
  const confirmUnlink = (u: UserOut) => {
    if (
      window.confirm(
        `Unlink SSO for ${u.email}? Their next SSO sign-in with this verified email links the account again, from any identity.`,
      )
    ) {
      setUnlinkError(null);
      unlink.mutate(u);
    }
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardTitle>Add a user</CardTitle>
        <div className="grid grid-cols-3 gap-4">
          <div className="space-y-1">
            <Label>Email</Label>
            <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label>Password</Label>
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} />
          </div>
          <div className="space-y-1">
            <Label>Role</Label>
            <select
              className="h-9 w-full rounded-md border border-border bg-card px-2 text-sm"
              value={role}
              onChange={(e) => setRole(e.target.value)}
            >
              <option value="member">member</option>
              <option value="admin">admin</option>
            </select>
          </div>
        </div>
        <Button className="mt-4" disabled={!email || password.length < 8 || create.isPending} onClick={() => { setError(null); create.mutate(); }}>
          Add user
        </Button>
        <ErrorText>{error}</ErrorText>
      </Card>

      <Card>
        <CardTitle>Users</CardTitle>
        <Table>
          <thead>
            <tr><Th>Email</Th><Th>Role</Th><Th>Auth</Th><Th>Created</Th><Th></Th></tr>
          </thead>
          <tbody>
            {(users.data ?? []).map((u) => (
              <tr key={u.id}>
                <Td>{u.email}</Td>
                <Td><Badge variant={u.role === "admin" ? "green" : "default"}>{u.role}</Badge></Td>
                <Td className="space-x-1">
                  {u.has_password && <Badge>password</Badge>}
                  {u.has_oidc && <Badge>oidc</Badge>}
                </Td>
                <Td>{new Date(u.created_at).toLocaleDateString()}</Td>
                <Td>
                  {u.has_oidc && (
                    <Button
                      size="sm"
                      variant="outline"
                      title={u.oidc_issuer ? `Linked through ${u.oidc_issuer}` : undefined}
                      disabled={unlink.isPending}
                      onClick={() => confirmUnlink(u)}
                    >
                      Unlink SSO
                    </Button>
                  )}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
        <ErrorText>{unlinkError}</ErrorText>
      </Card>
    </div>
  );
}
