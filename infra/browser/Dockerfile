FROM node:24-bookworm-slim
WORKDIR /app
ENV NODE_ENV=production
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY package.json package-lock.json ./
COPY apps/web/package.json apps/web/package.json
RUN npm ci --include=dev
RUN npx playwright install --with-deps chromium

COPY infra/browser/server.mjs infra/browser/server.mjs
RUN chown -R node:node /app /ms-playwright
USER node
EXPOSE 8080
CMD ["node", "infra/browser/server.mjs"]
