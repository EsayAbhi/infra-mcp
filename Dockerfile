FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[windows]"
ENV INFRA_MCP_CONFIG=/config/config.yaml
EXPOSE 8000
CMD ["infra-mcp", "--transport", "streamable-http"]
