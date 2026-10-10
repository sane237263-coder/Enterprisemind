# React static bundle and the single approved HTTPS edge.
ARG NODE_IMAGE
ARG NGINX_IMAGE
FROM ${NODE_IMAGE} AS build

WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN npm --prefix frontend ci --ignore-scripts
COPY frontend ./frontend
RUN npm --prefix frontend run build

FROM ${NGINX_IMAGE} AS runtime

COPY --from=build /src/frontend/dist /usr/share/nginx/html
COPY deploy/frontend/nginx.conf /usr/share/nginx/nginx.conf.template
COPY deploy/frontend/edge-entrypoint.sh /usr/local/bin/edge-entrypoint.sh
RUN chmod 0555 /usr/local/bin/edge-entrypoint.sh
USER 101:101

# Render the validated public origin to /tmp at runtime.  This entrypoint
# bypasses the image's /etc/nginx template scripts and never writes under /etc.
ENTRYPOINT ["/bin/sh", "/usr/local/bin/edge-entrypoint.sh"]
CMD ["-g", "daemon off;"]

# The container receives certificate files through Compose secrets at runtime.
EXPOSE 8080 8443
