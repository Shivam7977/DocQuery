FROM python:3.12-slim

# System dependency for OCR — this is the actual reason we need Docker at
# all; a plain "connect GitHub" Render deploy only installs Python packages,
# not OS-level binaries like tesseract.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Hugging Face Spaces runs Docker containers as user 1000. Create that user and
# run the app as it, so a bug or exploit cannot write anywhere outside /code/data.
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Cache the embedding and reranker models in the image, so the Space does not
# download them again after every restart (its disk is wiped on restart).
# HF_HOME is outside /root so the cache stays readable if the container later
# runs as a non-root user. The two loader files are the same ones the app uses,
# so the model names and the pinned reranker revision cannot drift apart.
ENV HF_HOME=/opt/hf-cache
COPY app/services/embeddings.py app/services/reranker.py /tmp/model_loaders/
RUN cd /tmp/model_loaders \
    && python -c "from embeddings import get_embedding_model; from reranker import get_reranker_model; get_embedding_model(); get_reranker_model(); print('models cached')" \
    && rm -rf /tmp/model_loaders \
    && chown -R user:user /opt/hf-cache

COPY --chown=user:user . .
RUN mkdir -p /code/data/uploads && chown -R user:user /code/data

# Port the platform routes traffic to. Hugging Face Spaces expects 7860;
# Render overrides PORT at runtime, so this default does not affect it.
ENV PORT=7860

USER user

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]