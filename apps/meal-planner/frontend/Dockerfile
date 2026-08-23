# Build context is apps/meal-planner/frontend.
FROM node:20-slim

ENV NODE_ENV=development \
    EXPO_NO_TELEMETRY=1

WORKDIR /app

# Dependency layer — only re-runs when the lockfile changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

EXPOSE 8081

CMD ["npx", "expo", "start", "--web", "--port", "8081"]
