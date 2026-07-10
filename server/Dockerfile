FROM python:3.12-slim

# ffmpeg(轉檔)+ Node.js 22(yt-dlp JS 挑戰求解與 PO Token 產生器都需要)+ git
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# PO Token 產生器(bgutil):讓 yt-dlp 不需帳號即可通過 YouTube 機器人驗證
# 版本必須與 requirements.txt 的 bgutil-ytdlp-pot-provider 外掛一致
RUN git clone --single-branch --branch 1.3.1 --depth 1 \
        https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil \
    && cd /opt/bgutil/server \
    && npm ci \
    && npx tsc \
    && npm prune --omit=dev \
    && npm cache clean --force

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py start.sh ./
RUN chmod +x start.sh

# 以非 root 使用者執行(暫存寫入系統 /tmp,不需額外權限)
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /opt/bgutil
USER app

ENV PORT=10000
# start.sh:啟動並監管 PO Token 產生器(死亡自動重啟),再 exec uvicorn
CMD ["./start.sh"]
