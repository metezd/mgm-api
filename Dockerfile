FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV APP_SERVER=waitress
ENV APP_HOST=0.0.0.0
ENV APP_PORT=5000

WORKDIR /app

COPY requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt

COPY . .

EXPOSE 5000

# uygulamanın kendi /health uç noktasını kullanır 
# start-period, ayağa kalkmasına imkan tanır
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; \
        urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('APP_PORT', '5000') + '/health', timeout=3)" \
    || exit 1

CMD ["python", "app.py"]
