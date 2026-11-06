import { useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";
import { api, ApiError } from "../../shared/api";
import { ACCOUNT_KEY } from "../orders/useOrders";

const ASSETS = ["USDT", "BTC", "ETH"];

export function DepositDialog({ onClose }: { onClose(): void }) {
  const client = useQueryClient();
  const [asset, setAsset] = useState(ASSETS[0]);
  const [amount, setAmount] = useState("");
  const [error, setError] = useState<string | null>(null);
  const key = useRef<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    key.current ??= crypto.randomUUID();
    try {
      await api("/api/accounts/deposits", {
        method: "POST",
        headers: { "Idempotency-Key": key.current },
        json: { asset, amount },
      });
      await client.invalidateQueries({ queryKey: ACCOUNT_KEY });
      onClose();
    } catch (err) {
      if (err instanceof ApiError) {
        key.current = null;
        setError(err.message);
      } else {
        setError("No reply from the server. Trying again is safe: it cannot deposit twice.");
      }
    }
  }

  return (
    <div className="fixed inset-0 z-10 flex items-center justify-center bg-harbour/70 p-4" role="dialog" aria-modal="true" aria-label="Deposit simulated funds">
      <form onSubmit={submit} className="w-full max-w-xs rounded-lg border border-line bg-panel p-5">
        <h2 className="text-base font-semibold">Deposit simulated funds</h2>
        <p className="mt-1 text-muted">Funds are credited at once. This is not real money.</p>
        <label className="mt-4 block">
          <span className="text-muted">Asset</span>
          <select value={asset} onChange={(e) => setAsset(e.target.value)} className="mt-1 w-full rounded border border-line bg-harbour px-2 py-2">
            {ASSETS.map((a) => (
              <option key={a}>{a}</option>
            ))}
          </select>
        </label>
        <label className="mt-3 block">
          <span className="text-muted">Amount</span>
          <input required autoFocus inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} className="mt-1 w-full rounded border border-line bg-harbour px-3 py-2" />
        </label>
        {error && (
          <p role="alert" className="mt-3 text-down">
            {error}
          </p>
        )}
        <div className="mt-4 flex gap-2">
          <button type="button" onClick={onClose} className="flex-1 rounded border border-line py-2">
            Close
          </button>
          <button className="flex-1 rounded bg-buoy py-2 font-semibold text-harbour">Deposit</button>
        </div>
      </form>
    </div>
  );
}
