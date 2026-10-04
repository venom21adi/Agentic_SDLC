# Image generated tests run in. Containers started from it have no network, so every dependency the
# generated code may import must be installed here.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest pydantic fastapi httpx sqlalchemy pyjwt bcrypt
WORKDIR /work
