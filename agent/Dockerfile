# Agent container image.
#   runtime (default): docker build -t bajiao-agent agent
#   test (CI, offline): docker build --target test -t bajiao-agent:test agent \
#                       && docker run --rm --network none bajiao-agent:test
FROM node:22-bookworm-slim AS mcp
# Two venvs are required: alpaca-mcp-server 2.4.1 pins fastmcp==3.4.7,
# openbb-mcp-server 2.0.1 needs fastmcp>=4.0.9.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/openbb \
 && /opt/openbb/bin/pip install --no-cache-dir \
      openbb-mcp-server==2.0.1 openbb-equity==2.0.0 openbb-derivatives==2.0.0 \
      openbb-index==2.0.0 openbb-economy==2.0.0 openbb-sec==2.0.0 \
      openbb-fred==2.0.0 openbb-yfinance==2.1.0
RUN python3 -m venv /opt/alpaca \
 && /opt/alpaca/bin/pip install --no-cache-dir alpaca-mcp-server==2.4.1
RUN ln -s /opt/openbb/bin/openbb-mcp /usr/local/bin/openbb-mcp \
 && ln -s /opt/alpaca/bin/alpaca-mcp-server /usr/local/bin/alpaca-mcp-server

FROM node:22-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY tsconfig.json vitest.config.ts ./
COPY src ./src
COPY test ./test
COPY scripts ./scripts
COPY skills ./skills
RUN npm run build

FROM mcp AS test
WORKDIR /app
COPY --from=build /app ./
ENTRYPOINT ["npm", "test"]

FROM build AS prod-deps
RUN npm prune --omit=dev --ignore-scripts

FROM mcp AS runtime
ARG APP_REVISION=unknown
LABEL io.bajiao.project="bajiao" \
      org.opencontainers.image.revision="${APP_REVISION}"
ENV NODE_ENV=production AGENT_DATA_DIR=/agent-data
WORKDIR /app
COPY package.json ./
COPY --from=prod-deps /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY skills ./skills
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:8080/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "dist/server.js"]
