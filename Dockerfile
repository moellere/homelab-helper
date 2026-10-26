# homelab-helper as a container: the `helper` CLI with the postgres extra, plus the two CLIs its
# Kubernetes and Talos adapters shell out to. Built by .github/workflows/image.yml and published to
# ghcr.io/moellere/homelab-helper; the cluster's CronJobs run discovery and the daemon from it.
FROM python:3.12-slim-bookworm AS build
RUN pip install --no-cache-dir uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv pip install --system --no-cache ".[postgres]"

FROM python:3.12-slim-bookworm
ARG KUBECTL_VERSION=v1.37.1
ARG TALOSCTL_VERSION=v1.14.2
ARG TARGETARCH=amd64
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" -o /usr/local/bin/kubectl \
    && curl -fsSL "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl.sha256" | sed 's#$#  /usr/local/bin/kubectl#' | sha256sum -c - \
    && curl -fsSL "https://github.com/siderolabs/talos/releases/download/${TALOSCTL_VERSION}/talosctl-linux-${TARGETARCH}" -o /usr/local/bin/talosctl \
    && chmod 0755 /usr/local/bin/kubectl /usr/local/bin/talosctl \
    && useradd --create-home --uid 1000 helper
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/helper /usr/local/bin/helper
USER helper
WORKDIR /home/helper
ENV HOMELAB_HELPER_HOME=/home/helper
ENTRYPOINT ["helper"]
CMD ["--help"]
