# AiRouter (9Router + Auto Tor IP changer) on Render
# Render sets PORT env (e.g. 10000). 9Router reads PORT + HOSTNAME.
# We run Tor control-port style (no auth) as debian-tor user.

FROM node:22-slim

ENV DEBIAN_FRONTEND=noninteractive \
    HOSTNAME=0.0.0.0 \
    PORT=10000 \
    INITIAL_PASSWORD=sakib \
    FREEBUFF_ENABLED=1 \
    FREEBUFF_BASE_URL=https://freebuff.llm.pm/v1 \
    PYTHONUNBUFFERED=1

# Install python3, tor, procps (for pkill), and clean up
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip tor procps ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Python deps for auto_tor.py (socks proxy support)
RUN pip3 install --no-cache-dir --break-system-packages "requests[socks]>=2.28.0"

# Install 9router globally (cached; npx also works but slower cold start)
RUN npm install -g 9router

# Tor: enable control port with no authentication (auto_tor sends bare AUTHENTICATE)
RUN echo "ControlPort 9051" >> /etc/tor/torrc && \
    echo "CookieAuthentication 0" >> /etc/tor/torrc && \
    mkdir -p /var/lib/tor && \
    chown -R debian-tor:debian-tor /var/lib/tor

WORKDIR /app
COPY requirements.txt ./
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt || true
COPY . .

# Render health check uses /healthz on PORT; 9Router dashboard serves the webpage.
# Tor keeps rotating IP every --interval seconds in the background.
# autoDeploy: true in render.yaml => push to main triggers new deploy automatically.
CMD sh -c "su -s /bin/bash debian-tor -c 'tor &' && sleep 2 && python3 run.py --router-mode npm --tor-method control --interval 3 --verbose"
