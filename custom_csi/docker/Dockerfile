FROM happysixd/osworld-docker@sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ebtables=2.0.11-6 \
    && update-alternatives --set ebtables /usr/sbin/ebtables-legacy \
    && test "$(readlink -f /usr/sbin/ebtables)" = /usr/sbin/ebtables-legacy \
    && test "$(readlink -f /usr/sbin/ebtables-save)" = /usr/sbin/ebtables-legacy-save \
    && test "$(readlink -f /usr/sbin/ebtables-restore)" = /usr/sbin/ebtables-legacy-restore \
    && rm -rf /var/lib/apt/lists/*
