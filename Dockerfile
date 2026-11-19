# Remote: clock, Claude Code sessions, Spotify and voice questions for a phone on the home network.
#   runtime (default)  the server, non-root, data in /data
#   test               the server plus pytest and Chromium: runs scripts/test.sh
FROM python:3.13-slim AS deps
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# openWakeWord's models (release v0.5.1). Their licence (CC BY-NC-SA 4.0) allows personal use; they are fetched here
# rather than kept in the repository, and checked against these hashes.
RUN mkdir models && python - <<'PY'
import hashlib, urllib.request
OWW = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
for name, sha in {
    "melspectrogram.onnx": "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    "embedding_model.onnx": "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
    "hey_jarvis_v0.1.onnx": "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
    "alexa_v0.1.onnx": "6ff566a01d12670e8d9e3c59da32651db1575d17272a601b7f8a39283dfbae3e",
    "hey_mycroft_v0.1.onnx": "c2a311e8fa1338de89c31b3b46dc4dffd4af2f9a8d6ddead48893c2d301b1f18",
    "hey_rhasspy_v0.1.onnx": "5a9b3ed3be2910e35780e097905aa9f35a9c10038df47914cf2b3ec4d670f6ea",
}.items():
    data = urllib.request.urlopen(OWW + name, timeout=60).read()
    assert hashlib.sha256(data).hexdigest() == sha, name
    open("models/" + name, "wb").write(data)
# about 10 hours of real speech, music and noise as openWakeWord features (same licence): negatives for training
# new wake words on the phone (server/trainer.py), and the yardstick for how often they wake by mistake
FEATURES = "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/validation_set_features.npy"
data = urllib.request.urlopen(FEATURES, timeout=600).read()
assert hashlib.sha256(data).hexdigest() == "a56a8a0f8e0efb91900acc6de4c0cdf4c564842e8475a7d49b36c039e17a690f"
open("models/validation_set_features.npy", "wb").write(data)
PY
# Piper voices for spoken answers (rhasspy/piper-voices v1.0.0), checked against these hashes.
RUN mkdir voices && python - <<'PY'
import hashlib, urllib.request
BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/"
for path, sha, sha_json in [
    ("en/en_US/hfc_male/medium/en_US-hfc_male-medium", "d11e403a02bdf5a670c877b3dc56e0e1c8cece6fb30289586314dffdc0a78cb0", "f66847424aed0bf99ecbb5d7cfde47c0a906f426a0daf7c46f305e7d21afd886"),
    ("en/en_GB/alan/medium/en_GB-alan-medium", "0a309668932205e762801f1efc2736cd4b0120329622adf62be09e56339d3330", "c0f0d124e5895c00e7c03b35dcc8287f319a6998a365b182deb5c8e752ee8c1e"),
    ("pt/pt_BR/faber/medium/pt_BR-faber-medium", "858555e3a064209c57088fe6bd70c4c3dc54d03eaa00c45d5ecaf43a33f95aa7", "7e694de195ae3fc36dd732c445eb04fb49b649854893cb5506b978f0d50a1d6f"),
    ("nl/nl_NL/mls/medium/nl_NL-mls-medium", "88312e0fbf505b87caf2373d94c1384892e86b1bf2ee482cf65dc8ba179cc7d3", "6ddb215d38f1392ab935ad45441b82ada1eeae0452a2d6849ed71ea4f2e0aa63"),
    # 904 speakers: the voices that say a new wake word when one is trained (server/trainer.py)
    ("en/en_US/libritts_r/medium/en_US-libritts_r-medium", "10bb85e071d616fcf4071f369f1799d0491492ab3c5d552ec19fb548fac13195", "b471dc60d2d8335e819c393d196d6fbf792817f40051257b269878505bc9afb3"),
    ("es/es_ES/davefx/medium/es_ES-davefx-medium", "6658b03b1a6c316ee4c265a9896abc1393353c2d9e1bca7d66c2c442e222a917", "0e0dda87c732f6f38771ff274a6380d9252f327dca77aa2963d5fbdf9ec54842"),
]:
    name = path.rsplit("/", 1)[1]
    for ext, want in ((".onnx", sha), (".onnx.json", sha_json)):
        data = urllib.request.urlopen(BASE + path + ext, timeout=120).read()
        assert hashlib.sha256(data).hexdigest() == want, name + ext
        open("voices/" + name + ext, "wb").write(data)
PY

FROM deps AS test
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright HOME=/tmp
COPY requirements-dev.txt .
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && pip install -r requirements-dev.txt \
 && playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*
COPY server ./server
COPY web ./web
COPY hooks ./hooks
COPY tests ./tests
COPY scripts ./scripts
USER 1000:1000
ENTRYPOINT ["sh", "scripts/test.sh"]

FROM deps AS runtime
# Claude Code (voice answers) from the official installer, copied out of root's home so the unprivileged user can run
# it. Its login is the server's own, in /data/claude; auto-update is off (rebuild to update).
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://claude.ai/install.sh | bash \
 && cp -L /root/.local/bin/claude /usr/local/bin/claude \
 && rm -rf /root/.local /root/.claude /root/.claude.json /var/lib/apt/lists/*
COPY server ./server
COPY web ./web
COPY hooks ./hooks
ENV REMOTE_DATA=/data HOME=/tmp CLAUDE_CONFIG_DIR=/data/claude DISABLE_AUTOUPDATER=1 REMOTE_HTTPS_PORT=8797
USER 1000:1000
EXPOSE 8796 8797
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8796/health',timeout=3)"
CMD ["python", "-m", "server", "--host", "0.0.0.0", "--port", "8796"]
