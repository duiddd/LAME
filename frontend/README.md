# LAME frontend

The application shell and trading page (Service A), embedding Service C's chat box and page.
Design: `docs/service_a_architecture.md`, section 13.

## Run

```bash
npm install
npm run dev            # needs A on :8000, B on :9999 and C on :8080
VITE_MOCK=1 npm run dev   # no backend: A's API and B's feed are simulated in the page
npm test
npm run build
```

`VITE_B_MD_URL` and `VITE_C_URL` override where the browser reaches B and C. The default is the
page's own host on ports 9999 and 8080.

`npm run gen:api` regenerates `src/shared/api-types.ts` from `openapi.json`, which is a copy of
`service_a/openapi.json`.
