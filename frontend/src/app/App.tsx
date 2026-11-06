import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, Link, Navigate, NavLink, Outlet, Route, Routes } from "react-router-dom";
import { DepositDialog } from "../features/account/DepositDialog";
import { AnalystPage } from "../features/analyst/Panels";
import { LoginPage } from "../features/auth/LoginPage";
import { useAuth } from "../features/auth/useAuth";
import { useUserStream } from "../features/orders/useOrders";
import { Logo } from "./Logo";
import { TradePage } from "./TradePage";

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: true, retry: 1 } },
});

function navClass({ isActive }: { isActive: boolean }) {
  return isActive ? "text-buoy" : "text-muted hover:text-ink";
}

function Shell() {
  const { user, signOut } = useAuth();
  const [depositing, setDepositing] = useState(false);
  useUserStream(user !== null);

  return (
    <div className="flex h-full flex-col">
      <nav className="flex flex-wrap items-center gap-5 border-b border-line bg-panel px-4 py-2" aria-label="Main">
        <Link to="/">
          <Logo />
        </Link>
        <NavLink to="/trade" className={navClass}>
          Trade
        </NavLink>
        <NavLink to="/news-analyst" className={navClass}>
          News & Analyst
        </NavLink>
        <div className="ml-auto flex items-center gap-4">
          {user ? (
            <>
              <button type="button" onClick={() => setDepositing(true)} className="rounded bg-buoy px-3 py-1 font-medium text-harbour">
                Deposit
              </button>
              <span className="text-muted">{user.username}</span>
              <button type="button" onClick={() => void signOut()} className="text-muted hover:text-ink">
                Log out
              </button>
            </>
          ) : (
            <Link to="/login" className="rounded bg-buoy px-3 py-1 font-medium text-harbour">
              Log in
            </Link>
          )}
        </div>
      </nav>
      <div className="min-h-0 flex-1 overflow-auto lg:overflow-hidden">
        <Outlet />
      </div>
      {depositing && <DepositDialog onClose={() => setDepositing(false)} />}
    </div>
  );
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route element={<Shell />}>
            <Route path="/trade/:symbol" element={<TradePage />} />
            <Route path="/trade" element={<Navigate to="/trade/BTC-USDT" replace />} />
            <Route path="/news-analyst" element={<AnalystPage />} />
            <Route path="*" element={<Navigate to="/trade/BTC-USDT" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
