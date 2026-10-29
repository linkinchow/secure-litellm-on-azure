ARG NODE_IMAGE=node@sha256:c86ecead9e55a5210ac1727de38a4ca840bf03dcaf34dc9749addbf383b1920c
FROM ${NODE_IMAGE}
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund && npm cache clean --force
COPY *.mjs ./
COPY native-ui-routes.json ./
USER 10001:10001
EXPOSE 8080
CMD ["node", "server.mjs"]