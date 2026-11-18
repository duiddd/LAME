import { type FormEvent, useState } from "react";
import { Navigate } from "react-router-dom";
import { Logo } from "../../app/Logo";
import { ApiError } from "../../shared/api";
import { useAuth } from "./useAuth";

export function LoginPage() {
  const { user, signIn } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) {
    return <Navigate to="/" replace />;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(mode, username, password);
    } catch (err) {
      if (err instanceof ApiError && err.reason === "USERNAME_TAKEN") {
        setError("That username is taken. Choose another one.");
      } else if (err instanceof ApiError && err.status === 401) {
        setError("The username or password is wrong.");
      } else if (err instanceof ApiError && err.status === 429) {
        setError("Too many attempts. Wait a moment and try again.");
      } else if (err instanceof ApiError && err.reason === "INVALID_REQUEST") {
        setError("Use a username of 3 to 32 letters or digits, and a password of at least 8 characters.");
      } else {
        setError("The service cannot be reached. Try again shortly.");
      }
    } finally {
      setBusy(false);
    }
  }

  const title = mode === "login" ? "Log in" : "Create account";
  return (
    <main className="flex min-h-full items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm rounded-lg border border-line bg-panel p-6">
        <Logo large />
        <h1 className="mt-6 text-xl font-semibold">{title}</h1>
        <p className="mt-1 text-muted">Practice spot trading with simulated funds. No personal details needed.</p>
        <label className="mt-5 block">
          <span className="text-muted">Username</span>
          <input
            required
            autoComplete="username"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            className="mt-1 w-full rounded border border-line bg-harbour px-3 py-2"
          />
        </label>
        <label className="mt-3 block">
          <span className="text-muted">Password</span>
          <input
            required
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1 w-full rounded border border-line bg-harbour px-3 py-2"
          />
        </label>
        {error && (
          <p role="alert" className="mt-3 text-down">
            {error}
          </p>
        )}
        <button disabled={busy} className="mt-5 w-full rounded bg-buoy py-2 font-semibold text-harbour disabled:opacity-50">
          {title}
        </button>
        <button
          type="button"
          onClick={() => {
            setMode(mode === "login" ? "register" : "login");
            setError(null);
          }}
          className="mt-3 w-full text-muted hover:text-ink"
        >
          {mode === "login" ? "Create an account instead" : "Log in to an existing account"}
        </button>
      </form>
    </main>
  );
}
