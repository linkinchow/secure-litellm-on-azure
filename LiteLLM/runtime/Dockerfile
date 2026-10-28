FROM docker.litellm.ai/berriai/litellm@sha256:6cc08e60be3e4ebf0cfb41ef23aed97e9ba4f61702d1e95e533f47ee6d67865f

ADD --checksum=sha256:9f505dda5ac9f0c8309b5e8bd445a8c2bf7246f3ce950121e45ea15bc41d1494 \
    https://files.pythonhosted.org/packages/da/35/f2287558c17e29fafc8ef3daf819bb9834061cfa43bff8014f7df7f63bdc/anyio-4.14.2-py3-none-any.whl \
    /tmp/anyio-4.14.2-py3-none-any.whl
ADD --checksum=sha256:42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b \
    https://files.pythonhosted.org/packages/c4/51/29046e48f693c54e5b7412216d447a1d8362c9653148d98c02cdf65fa733/certifi-2026.10.19-py3-none-any.whl \
    /tmp/certifi-2026.10.19-py3-none-any.whl
COPY LiteLLM/runtime/security-requirements.txt /tmp/security-requirements.txt
RUN /app/.venv/bin/python -m ensurepip \
    && /app/.venv/bin/python -m pip install --no-cache-dir --no-deps --no-index \
        --find-links /tmp --require-hashes --requirement /tmp/security-requirements.txt \
    && /app/.venv/bin/python -m pip uninstall --yes pip \
    && rm /tmp/anyio-4.14.2-py3-none-any.whl /tmp/certifi-2026.10.19-py3-none-any.whl /tmp/security-requirements.txt

COPY --chown=10001:10001 LiteLLM/runtime /opt/llmgw/LiteLLM/runtime
ENV PYTHONPATH=/opt/llmgw \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp \
    LITELLM_LOCAL_MODEL_COST_MAP=True
USER 10001:10001
ENTRYPOINT ["/app/.venv/bin/python", "-m", "LiteLLM.runtime.application"]
CMD ["--config", "/app/config/config.yaml"]