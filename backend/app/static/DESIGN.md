# 八角 · 波段工作台 — design contract

## Mission
A calm, auditable financial workspace for monitoring the original eight Binance token pairs and researching their underlying US equities over a 2–4 week horizon. Analysis informs research, never promises returns or executes trades.

## Tokens
- Canvas `#f3f5f7`; surface `#ffffff`; navigation `#17252d`; ink `#182b35`; muted `#596b75`; border `#dde5e9`.
- Primary teal `#13695e`; informational blue `#245c88`; warning `#875516`; loss/error `#ad3842`; gain `#13695e`. Status always includes text, not color alone.
- System sans: -apple-system, BlinkMacSystemFont, Segoe UI, PingFang SC, Microsoft YaHei. Financial numerals: system monospace, tabular numbers.
- Type: 12px captions, 14px body/control, 16px section headings, 28–32px editorial titles.
- Space: 4/8/12/16/24/32/40px. Radius: 6px controls, 10px panels, 12px dialogs.
- Motion: immediate color feedback; 180ms press/opacity; 260ms cubic-bezier(.2,.8,.2,1) state entrances with 24ms capped stagger. Reduced motion removes entrances and transitions.

## Do
- Stable, readable tables; semantic forms; visible keyboard focus; native dialog focus containment.
- Explicit loading, missing configuration, empty results, errors and saved state. No synthetic chart, price, performance, delivery status or demo records.
- Label research prices as underlying-equity USD, separately from executable Binance prices. Track candidate transfers symbol only.
- Render API content as text nodes; accept source URLs only over HTTP(S); never persist credentials in browser storage.
- Keep original eight symbols only; server status supplies their live scope/configuration.

## Don't
- External runtime libraries/fonts/CDNs, decorative motion, forecast certainty, probability labels for screening scores, automatic research-price transfer, or generic landing-page sections.
- Treat failed notification as delivered; offer default repeat push after success; silently swallow API errors.

## Assumptions
- FastAPI serves this directory at `/`; API responses follow the delegated contract. HTTP Basic, when enabled, remains browser-managed.
- Research entry/target/stop prices are displayed only with explicit `US_EQUITY_USD` basis; legacy prices remain hidden.
- No authentication, trading/order execution, extra symbols or quote-history chart is implemented.
- Clearing an optional position target/stop submits null; backend must honor explicit null for clearing.
