import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, errorDetail } from "../lib/api";
import { Button, Card, CardTitle, ErrorText, Input, Label } from "../components/ui";

export default function Login({ onLogin }: { onLogin: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const methods = useQuery({
    queryKey: ["auth-methods"],
    queryFn: async () => {
      const { data, error } = await api.GET("/auth/methods");
      if (error) throw error;
      return data;
    },
  });

  return (
    <div className="flex min-h-screen items-center justify-center">
      <Card className="w-96">
        <CardTitle>Sign in to Purser</CardTitle>
        <form
          className="space-y-4"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError(null);
            const { error: err } = await api.POST("/auth/login", {
              body: { email, password },
            });
            setBusy(false);
            if (err) setError(errorDetail(err));
            else onLogin();
          }}
        >
          <div className="space-y-1">
            <Label htmlFor="email">Email</Label>
            <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <div className="space-y-1">
            <Label htmlFor="password">Password</Label>
            <Input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </div>
          <Button type="submit" className="w-full" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
          <ErrorText>{error}</ErrorText>
        </form>
        {methods.data?.oidc && (
          <Button
            type="button"
            variant="outline"
            className="mt-4 w-full"
            onClick={() => {
              window.location.href = "/auth/oidc/login";
            }}
          >
            Sign in with SSO
          </Button>
        )}
      </Card>
    </div>
  );
}
