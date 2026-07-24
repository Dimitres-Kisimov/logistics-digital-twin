# OR-Tools ships manylinux wheels for CPython 3.12, so we pin the slim 3.12 base for reliable
# `pip install ortools`. The image runs the Flask app via gunicorn on port 5000.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# gunicorn serves the WSGI app object `app` from app.py.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--timeout", "120", "app:app"]
