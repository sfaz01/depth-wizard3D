"""
app.py - Entry point for Hugging Face Spaces (Gradio/Python SDK, free CPU tier).

Spaces runs `python app.py` and expects a web server on port 7860. We don't use Gradio's
UI; this simply starts the DepthWizard FastAPI server, which serves the full web app.
"""

import os

import uvicorn

if __name__ == "__main__":
    os.environ.setdefault("DW_PORT", "7860")
    uvicorn.run("api.main:app", host="0.0.0.0", port=int(os.environ["DW_PORT"]))
