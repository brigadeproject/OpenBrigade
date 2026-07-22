FROM node:22-alpine AS web-build

WORKDIR /app/web
COPY web/package.json web/package-lock.json web/tsconfig.json web/vite.config.ts web/index.html web/mobile.html ./
COPY web/src ./src
COPY web/public ./public
RUN npm ci
RUN npm run build

FROM python:3.11-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY brigade ./brigade
COPY migrations ./migrations
COPY --from=web-build /app/web/dist ./web/dist
RUN python -m pip install --no-cache-dir ".[web,models,ingest]"

ENTRYPOINT ["brigade"]
CMD ["dashboard"]
