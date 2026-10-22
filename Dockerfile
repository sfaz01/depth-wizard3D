# CPU image. Works on Hugging Face Spaces (Docker SDK, port 7860) and any container host.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DW_PORT=7860 DW_HOST=0.0.0.0 \
    HF_HOME=/app/data/cache/hf
WORKDIR /app

RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Bake the model weights into the image so the first scene does not wait on a download.
RUN python -c "from transformers import pipeline; pipeline('depth-estimation', model='depth-anything/Depth-Anything-V2-Small-hf')" \
 && useradd -m -u 1000 app && chown -R app /app
USER app

EXPOSE 7860
CMD ["python", "-m", "api.main"]
