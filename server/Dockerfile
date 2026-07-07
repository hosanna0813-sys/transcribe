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
COPY main.py .

ENV PORT=10000
# 先啟動 PO Token 產生器(4416 埠,yt-dlp 外掛自動連線),再啟動 API
CMD ["sh", "-c", "node /opt/bgutil/server/build/main.js & exec uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
