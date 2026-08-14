FROM python:3.12-slim

WORKDIR /app

# Системные библиотеки для PyMuPDF и Docling (PDF-парсинг)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

# Зависимости Python (кешируем слой отдельно от кода)
COPY bot/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код системы
COPY . .

EXPOSE 8000

CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
