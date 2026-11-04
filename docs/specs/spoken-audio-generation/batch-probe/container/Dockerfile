# Cold-start probe image (Appendix C): the PR-0 image plus a one-shot Batch
# entry point. Torch's cu128 wheels carry their own CUDA runtime, so a slim
# Python base is enough; the host AMI supplies the driver.
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.9.5
COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache --index-strategy unsafe-best-match \
      --extra-index-url https://download.pytorch.org/whl/cu128 -r /app/requirements.txt
COPY generation_utils.py /app/moss/generation_utils.py
COPY serve.py batch_job.py /app/

ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python"]
CMD ["/app/serve.py"]
