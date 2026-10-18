# syntax=docker/dockerfile:1
# Disposable single-node scheduler qualification, never a production cluster.
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
ARG DEBIAN_SNAPSHOT=20261013T000000Z
RUN --mount=type=secret,id=proxy_ca,mode=0444 \
    set -eu; \
    sed -i "s|http://deb.debian.org/debian-security|https://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|; s|http://deb.debian.org/debian|https://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|" /etc/apt/sources.list.d/debian.sources; \
    if [ -f /run/secrets/proxy_ca ]; then printf 'Acquire::https::CaInfo "/run/secrets/proxy_ca";\n' > /etc/apt/apt.conf.d/99raven-ca; fi; \
    apt-get -o Acquire::Check-Valid-Until=false update && DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Check-Valid-Until=false install -y --no-install-recommends \
    build-essential ca-certificates curl bzip2 munge libmunge-dev libpam0g-dev \
    libhwloc-dev libjson-c-dev libyaml-dev libdbus-1-dev libssl-dev \
    libmariadb-dev mariadb-server python3 procps tini && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99raven-ca
ARG SLURM_VERSION=26.05.5
ARG SLURM_SHA256=4680cafc82b0689865c885eb703441e7ebf764896c40fd03a50f2fcb60664f68
ARG BUILD_JOBS=2
RUN --mount=type=secret,id=proxy_ca,mode=0444 \
    set -eu; \
    if [ -f /run/secrets/proxy_ca ]; then export CURL_CA_BUNDLE=/run/secrets/proxy_ca; fi; \
    curl --fail --location --retry 3 "https://download.schedmd.com/slurm/slurm-${SLURM_VERSION}.tar.bz2" -o /tmp/slurm.tar.bz2; \
    printf '%s  %s\n' "$SLURM_SHA256" /tmp/slurm.tar.bz2 | sha256sum -c -; \
    mkdir /tmp/slurm-source; tar -xjf /tmp/slurm.tar.bz2 --strip-components=1 -C /tmp/slurm-source; \
    cd /tmp/slurm-source; ./configure --prefix=/opt/slurm --sysconfdir=/etc/slurm --disable-dependency-tracking --without-hdf5; \
    make -j "$BUILD_JOBS"; make install; \
    rm -rf /tmp/slurm-source /tmp/slurm.tar.bz2
ENV PATH=/opt/slurm/bin:/opt/slurm/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
RUN useradd --uid 1100 --create-home raven && useradd --system slurm && \
    mkdir -p /etc/slurm /var/spool/slurmctld /var/spool/slurmd /var/log/slurm /run/munge /run/mysqld && \
    chown munge:munge /run/munge && chown mysql:mysql /run/mysqld && \
    chown slurm:slurm /var/spool/slurmctld /var/log/slurm
COPY --chmod=755 entrypoint.sh /usr/local/bin/raven-slurm-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/raven-slurm-entrypoint"]
