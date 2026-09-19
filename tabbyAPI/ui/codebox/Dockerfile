FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        python3 \
        python3-pip \
        python3-venv \
        sudo \
        unzip \
    && PIP_BREAK_SYSTEM_PACKAGES=1 pip3 install --no-cache-dir "python-lsp-server" \
    && npm install -g --omit=dev --no-fund --no-audit \
        typescript-language-server vscode-langservers-extracted \
    && printf '%s\n' \
        'Defaults env_reset,!lecture,!requiretty' \
        'ALL ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt' \
        > /etc/sudoers.d/tabby-apt \
    && chmod 0440 /etc/sudoers.d/tabby-apt \
    && rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache

ENV PATH="/work/.venv/bin:/work/.local/bin:${PATH}"

WORKDIR /work
CMD ["sleep", "infinity"]
