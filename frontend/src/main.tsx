import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { setSocketFactory } from "./features/market/useFeed";
import "./index.css";

async function start() {
  // Mocks are loaded only in mock mode, so they never reach the production bundle's path.
  if (import.meta.env.VITE_MOCK === "1") {
    const [{ installMockApi }, { FakeSocket }] = await Promise.all([import("./mocks/browser"), import("./mocks/fakeSocket")]);
    setSocketFactory((url) => new FakeSocket(url));
    installMockApi();
  }
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void start();
